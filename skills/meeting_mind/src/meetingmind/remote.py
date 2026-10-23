"""Client side of `--backend spark`: send the audio to the lab server, get text back.

One meeting is one job directory on the server (layout in `server.py`):

  1. Compress. The 48 kHz stereo WAV becomes 16 kHz mono FLAC. The model only
     ever sees 16 kHz mono — locally, librosa resamples on load with soxr, the
     resampler used here — so nothing it uses is lost, while an hour of audio
     shrinks from ~660 MB to 30-40 MB (measured 2026-10-23).
  2. `new-job`, then upload the FLACs and job.json, which carries every file's
     sha256.
  3. `submit`. The server checks the sha256s before queueing, so a damaged
     upload is caught and sent again instead of being transcribed.
  4. Poll `status` until the job ends, printing queue position / chunk.
  5. Download result.json into the meeting directory, then `clean` the job
     directory on the server, and hand back the segments.

Two files in `<meeting_dir>/transcript/` make every step resumable:

  - `spark_job.json` — written as soon as the server directory exists, so an
    interruption at any later point (closed terminal, lost Claude session,
    Ctrl+C, a submit whose reply never arrived) is picked up by running
    postprocess again, instead of submitting a second job or orphaning one.
  - `spark_result.json` — the fetched result, saved *before* the server copy
    is deleted, so nothing finished is ever lost on the way to transcript.md.

Nothing is cleaned up on the strength of a guess: only a state Slurm reports as
final lets the client delete a job directory (and the server refuses to delete
one whose job is still queued or running).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .transcribe import Segment

#: Must match `server.PROTOCOL` on the deployed server.
PROTOCOL = 1
STATE_FILE = Path("transcript") / "spark_job.json"
RESULT_FILE = Path("transcript") / "spark_result.json"
TARGET_RATE = 16_000
POLL_PENDING = 30.0
POLL_RUNNING = 10.0
UPLOAD_ATTEMPTS = 3
#: Consecutive failed status polls tolerated (Wi-Fi blip, laptop lid, slurmctld
#: busy) before giving up — the job itself keeps running regardless.
POLL_FAILURES = 5
#: How long `spark-cancel` waits for a cancelled job to leave the GPU.
CANCEL_WAIT_SECONDS = 45
SSH_OPTIONS = (
    "-o", "BatchMode=yes",  # never hang on a password prompt nobody will see
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
)

_ACTIVE = frozenset({"PENDING", "CONFIGURING", "RUNNING", "COMPLETING", "SUSPENDED", "REQUEUED"})
#: Final states, after which the job directory may go.
_FINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
                    "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"})
_SAFE_REMOTE = re.compile(r"^/[A-Za-z0-9._/-]+$")
#: Where an unfinished server transcript lands — the names local runs use.
_PARTIAL_NAMES = {"main": "_partial.md", "mic": "_partial_mic.md"}


class RemoteError(RuntimeError):
    """The server, ssh or scp said no. `data` is the server's JSON, if any."""

    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.data = data or {}

    @property
    def transient(self) -> bool:
        """No verdict from the server: the network, or Slurm itself, did not answer."""
        return not self.data or self.data.get("error") == "slurm_unavailable"


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _last_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.strip().splitlines()):
        with contextlib.suppress(ValueError):
            data = json.loads(line)
            if isinstance(data, dict):
                return data
    return {}


def _check_remote(path: str) -> None:
    # The server builds these paths; refusing anything odd keeps a surprise in
    # its output from turning into a shell or scp argument on either side.
    if not _SAFE_REMOTE.match(path) or "/../" in f"{path}/":
        raise RemoteError(f"unexpected server path: {path!r}")


@dataclass
class SparkClient:
    host: str
    command: str
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def _exec(self, argv: list[str], timeout: float, cwd: Path | None = None) -> subprocess.CompletedProcess:
        try:
            return self.runner(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, cwd=str(cwd) if cwd else None,
            )
        except FileNotFoundError as exc:
            raise RemoteError(
                f"'{argv[0]}' is not installed on this machine"
                f" / 本机找不到 {argv[0]} 命令（需要 OpenSSH 客户端）"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(
                f"{self.host} did not answer within {timeout:.0f} s"
                f" / {self.host} 在 {timeout:.0f} 秒内没有响应"
            ) from exc

    def call(self, *args: str, timeout: float = 60) -> dict[str, Any]:
        """Run one `meetingmind-asr` subcommand on the server and return its JSON."""
        remote = " ".join(shlex.quote(a) for a in (self.command, *args))
        cp = self._exec(["ssh", *SSH_OPTIONS, self.host, remote], timeout)
        if cp.returncode == 255:
            raise RemoteError(
                f"Cannot reach {self.host} over ssh: {cp.stderr.strip()[-300:]}\n"
                f"  Check that Tailscale is connected and `ssh {self.host}` logs in "
                f"without a password."
                f" / 连不上 {self.host}：检查 Tailscale 是否在线，"
                f"以及 `ssh {self.host}` 能否免密登录。"
            )
        if cp.returncode == 127:
            raise RemoteError(
                f"{self.command} is not installed on {self.host}: server-side transcription "
                f"is not set up there; ask an admin."
                f" / {self.host} 上没有装服务器转录（找不到 {self.command}），请联系管理员。",
                {"error": "not_deployed"},
            )
        data = _last_json(cp.stdout)
        if cp.returncode != 0:
            detail = data.get("message") or cp.stderr.strip()[-500:] or f"exit {cp.returncode}"
            raise RemoteError(f"spark: {detail}", data)
        return data

    def _scp(self, src: str, dst: str, cwd: Path, timeout: float) -> None:
        # Local side is a bare file name with cwd set to its folder: a Windows
        # path like C:\... would otherwise be read by scp as host "C".
        cp = self._exec(["scp", "-q", *SSH_OPTIONS, src, dst], timeout, cwd=cwd)
        if cp.returncode != 0:
            raise RemoteError(
                f"scp failed: {cp.stderr.strip()[-300:]} / 文件传输失败"
            )

    def upload(self, local: Path, remote_path: str, timeout: float = 1800) -> None:
        _check_remote(remote_path)
        self._scp(local.name, f"{self.host}:{remote_path}", local.parent, timeout)

    def download(self, remote_path: str, local: Path, timeout: float = 600) -> None:
        _check_remote(remote_path)
        self._scp(f"{self.host}:{remote_path}", local.name, local.parent, timeout)


# --------------------------------------------------------------------- audio


def prepare_flac(wav: Path, out: Path) -> float:
    """Write `wav` as 16 kHz mono FLAC to `out`, streaming; return its seconds.

    Channels are averaged, as librosa.load(mono=True) does for the local path.
    Clipping before the int16 encode matters: resampling can overshoot ±1.0 on
    loud passages, and libsndfile would wrap those samples into clicks.
    """
    import numpy as np  # noqa: PLC0415 — keep `import meetingmind.remote` cheap
    import soundfile as sf  # noqa: PLC0415
    import soxr  # noqa: PLC0415

    info = sf.info(str(wav))
    stream = soxr.ResampleStream(info.samplerate, TARGET_RATE, 1, dtype="float32", quality="HQ")
    frames = 0
    with sf.SoundFile(str(out), "w", samplerate=TARGET_RATE, channels=1,
                      format="FLAC", subtype="PCM_16") as dst:
        blocks = sf.blocks(str(wav), blocksize=info.samplerate * 30, dtype="float32", always_2d=True)
        for block in blocks:
            y = stream.resample_chunk(block.mean(axis=1).astype(np.float32))
            dst.write(np.clip(y, -1.0, 1.0))
            frames += len(y)
        y = stream.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
        dst.write(np.clip(y, -1.0, 1.0))
        frames += len(y)
    return frames / TARGET_RATE


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fingerprint(files: list[tuple[str, Path]], vocab_context: str, chunk_minutes: float) -> str:
    """Identify the inputs of a job without re-reading the audio."""
    h = hashlib.sha256()
    for role, wav in files:
        st = wav.stat()
        h.update(f"{role}|{wav.name}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    h.update(f"{chunk_minutes!r}\n".encode())
    h.update(vocab_context.encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------------------- local state


def _load_json(path: Path) -> dict[str, Any] | None:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    return None


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _segments(result: dict[str, Any]) -> dict[str, list[Segment]]:
    if result.get("protocol") != PROTOCOL:
        raise RemoteError(f"result has protocol {result.get('protocol')!r}, expected {PROTOCOL}")
    return {
        role: [
            Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]),
                    speaker=s.get("speaker"))
            for s in segs
        ]
        for role, segs in (result.get("segments") or {}).items()
    }


def describe(status: dict[str, Any]) -> str:
    """One progress line for a `status` reply."""
    state = status.get("state", "?")
    if state == "PENDING":
        ahead = status.get("ahead")
        if ahead is None:
            queue = ""
        elif ahead:
            queue = f"，前面约有 {ahead} 个 GPU 作业"
        else:
            queue = "，下一个就是它"
        return f"[spark] 排队中（{status.get('reason', '')}）{queue}"
    if state in ("CONFIGURING", "RUNNING", "COMPLETING"):
        elapsed = status.get("elapsed", "?")
        progress = status.get("progress")
        if progress:
            return f"[spark] 转录中：第 {progress} 块（已用 {elapsed}）"
        return f"[spark] 作业已开始，正在加载模型（已用 {elapsed}）"
    if state == "FROZEN":
        return "[spark] 作业被 spark-memguard 冻结了"
    return f"[spark] 作业状态：{state}"


# --------------------------------------------------------------------- lifecycle


def _clean(client: SparkClient, job_dir: str) -> bool:
    try:
        client.call("clean", job_dir)
        return True
    except RemoteError as exc:
        _log(f"[spark] 服务器上的作业目录这次没删掉（7 天后会自动清理）：{exc}")
        return False


def _resume(state: dict[str, Any], client: SparkClient) -> str | None:
    """The job directory to keep working on, or None to start afresh."""
    job_dir = str(state.get("job_dir", ""))
    if not _SAFE_REMOTE.match(job_dir):
        return None
    try:
        status = client.call("status", job_dir)
    except RemoteError as exc:
        if exc.data.get("error") == "bad_path":
            _log("[spark] 上次的作业目录已不在服务器上，重新提交")
            return None
        raise  # network or Slurm did not answer: decide nothing, keep spark_job.json
    st = status.get("state")
    if status.get("result_ready") or st in _ACTIVE or st == "FROZEN":
        _log(f"[spark] 接着等上次提交的作业 {status.get('job_id')}（不重复提交）")
        return job_dir
    if st == "NOT_SUBMITTED" or st in _FINAL:
        why = "没交上去" if st == "NOT_SUBMITTED" else f"以 {st} 结束、没有结果"
        _log(f"[spark] 上次的作业{why}，重新提交")
        _clean(client, job_dir)
        return None
    raise RemoteError(
        f"Slurm has no record of spark job {status.get('job_id')} (state {st}); not touching it. "
        f"Run `spark-cancel` on this meeting to drop it, then postprocess again."
        f" / 服务器查不到作业 {status.get('job_id')} 的状态，先不动它；"
        f"确认不要了就对这场会跑 spark-cancel，再重跑 postprocess。"
    )


def _submit(
    meeting_dir: Path,
    files: list[tuple[str, Path]],
    vocab_context: str,
    chunk_minutes: float,
    client: SparkClient,
    state_path: Path,
    fp: str,
) -> str:
    with tempfile.TemporaryDirectory(prefix="meetingmind_spark_") as tmp:
        tmp_dir = Path(tmp)
        entries: list[dict[str, Any]] = []
        _log("[spark] 压缩音频（16 kHz 单声道 FLAC）…")
        for role, wav in files:
            flac = tmp_dir / f"{role}.flac"
            seconds = prepare_flac(wav, flac)
            if seconds <= 0:
                raise RuntimeError(f"{wav} contains no audio / 音频文件是空的：{wav}")
            entries.append({"role": role, "file": flac.name, "sha256": _sha256(flac),
                            "seconds": round(seconds, 2)})
            _log(f"[spark]   {wav.name}: {seconds / 60:.1f} 分钟 → "
                 f"{flac.stat().st_size / 1048576:.1f} MB")
        job = {
            "protocol": PROTOCOL,
            "client_version": __version__,
            "meeting": meeting_dir.name,
            "audio": entries,
            "vocab_context": vocab_context,
            "chunk_minutes": chunk_minutes,
        }
        (tmp_dir / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

        created = client.call("new-job", meeting_dir.name)
        if created.get("protocol") != PROTOCOL:
            raise RemoteError(
                f"server speaks protocol {created.get('protocol')!r}, this skill {PROTOCOL}: "
                "update the skill, or ask an admin to update the server copy"
                " / 服务器端和本机 skill 版本不匹配：更新 skill，或请管理员更新服务器端"
            )
        job_dir = str(created.get("job_dir", ""))
        _check_remote(job_dir)
        # From here on the server holds something of ours: record it first, so
        # no interruption below can leave a directory nobody knows about.
        state = {"job_dir": job_dir, "fingerprint": fp, "phase": "uploading",
                 "created_at": datetime.now().isoformat(timespec="seconds")}
        _save_json(state_path, state)

        submitted: dict[str, Any] = {}
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            _log("[spark] 上传到 spark …" if attempt == 1 else f"[spark] 重新上传（第 {attempt} 次）…")
            try:
                for entry in entries:
                    client.upload(tmp_dir / entry["file"], f"{job_dir}/{entry['file']}")
                client.upload(tmp_dir / "job.json", f"{job_dir}/job.json")
            except RemoteError:
                # Not submitted, certainly: take the partial upload back down.
                if _clean(client, job_dir):
                    state_path.unlink(missing_ok=True)
                raise
            try:
                submitted = client.call("submit", job_dir)
                break
            except RemoteError as exc:
                if exc.data.get("error") == "checksum" and attempt < UPLOAD_ATTEMPTS:
                    _log(f"[spark] {exc.data.get('file')} 上传后校验不一致")
                    continue
                if exc.transient:
                    # sbatch may or may not have run. Keep the state: the next
                    # run asks the server before doing anything.
                    _save_json(state_path, {**state, "phase": "submitting"})
                    raise RemoteError(
                        f"{exc}\n  Whether the job was queued is unknown; run the same command "
                        "again and it will check before submitting anything."
                        " / 不确定作业交上去没有；再跑同一条命令，会先查清楚再决定，不会重复提交。"
                    ) from exc
                # The server refused outright: nothing was queued.
                if _clean(client, job_dir):
                    state_path.unlink(missing_ok=True)
                raise

    _save_json(state_path, {**state, "phase": "submitted", "job_id": submitted.get("job_id")})
    _log(f"[spark] 已提交作业 {submitted.get('job_id')}（时限 {submitted.get('time_limit', '?')}）。"
         "中途断开也没关系：再跑同一条命令会接着等，不会重复提交。")
    return job_dir


def _wait(job_dir: str, client: SparkClient, sleep: Callable[[float], None]) -> dict[str, Any]:
    last = None
    failures = 0
    while True:
        try:
            status = client.call("status", job_dir)
        except RemoteError as exc:
            if not exc.transient:  # the server answered and refused
                raise
            failures += 1
            if failures >= POLL_FAILURES:
                raise RemoteError(
                    f"{exc}\n  The job keeps running on spark; run the same command "
                    "again once the connection is back. / 作业仍在 spark 上运行，"
                    "恢复后再跑同一条命令会接着等。"
                ) from exc
            _log(f"[spark] 暂时查不到状态，{POLL_RUNNING:.0f} 秒后重试（{failures}/{POLL_FAILURES}）")
            sleep(POLL_RUNNING)
            continue
        failures = 0
        line = describe(status)
        if line != last:
            _log(line)
            last = line
        state = status.get("state")
        if status.get("result_ready") or state in _FINAL or state in ("FROZEN", "UNKNOWN",
                                                                       "NOT_SUBMITTED"):
            return status
        sleep(POLL_PENDING if state == "PENDING" else POLL_RUNNING)


def _fail(meeting_dir: Path, job_dir: str, status: dict[str, Any], client: SparkClient,
          state_path: Path) -> None:
    job_id = status.get("job_id")
    state = status.get("state")
    if state == "FROZEN":
        # Keep spark_job.json: once the owner has dealt with it, a rerun sees a
        # finished job without a result and submits again.
        raise RuntimeError(
            f"spark job {job_id} was frozen by spark-memguard (the server ran short of "
            f"memory). Cancel it on spark with `advise.py cancel-frozen {job_id}` (plain "
            f"scancel would leave it stuck), then run postprocess again."
            f" / spark 作业 {job_id} 被 memguard 冻结（服务器内存吃紧）。"
            f"在 spark 上用 `advise.py cancel-frozen {job_id}` 取消（不要直接 scancel），"
            f"然后重跑 postprocess。"
        )
    if state not in _FINAL:
        # UNKNOWN / NOT_SUBMITTED: no final word from Slurm, so delete nothing.
        raise RuntimeError(
            f"spark job {job_id} is in state {state} and has no result; leaving it alone. "
            f"Run postprocess again later, or `spark-cancel` to drop it."
            f" / spark 作业 {job_id} 状态是 {state}、没有结果，先不动它。"
            f"稍后重跑 postprocess，或用 spark-cancel 撤掉。"
        )
    saved = []
    for role, name in _PARTIAL_NAMES.items():
        target = meeting_dir / "transcript" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(RemoteError):
            client.download(f"{job_dir}/partial_{role}.md", target)
            saved.append(str(target))
    _clean(client, job_dir)
    state_path.unlink(missing_ok=True)
    tail = status.get("log_tail") or ""
    partial = f"\nPartial transcript / 已转好的部分：{', '.join(saved)}" if saved else ""
    raise RuntimeError(
        f"spark job {job_id} ended as {state} without a result.\n{tail}{partial}"
        f"\n / spark 作业 {job_id} 以 {state} 结束，没有拿到结果。"
    )


def _collect(meeting_dir: Path, job_dir: str, client: SparkClient, state_path: Path,
             fp: str) -> dict[str, list[Segment]]:
    with tempfile.TemporaryDirectory(prefix="meetingmind_spark_") as tmp:
        local = Path(tmp) / "result.json"
        client.download(f"{job_dir}/result.json", local)
        result = json.loads(local.read_text(encoding="utf-8"))
    segments = _segments(result)
    # Saved before the server copy goes: a crash between here and transcript.md
    # must not cost the result.
    _save_json(meeting_dir / RESULT_FILE, {"fingerprint": fp, "result": result})
    _clean(client, job_dir)
    state_path.unlink(missing_ok=True)
    _log(f"[spark] 转录完成，服务器用时 {result.get('elapsed_seconds')} 秒")
    return segments


def cancel_on_spark(meeting_dir: Path, client: SparkClient,
                    sleep: Callable[[float], None] = time.sleep) -> str:
    """Withdraw this meeting's spark job (queued or running) and delete its files there.

    For switching to local transcription while the GPU queue is long: the job
    would otherwise still run later and hold the GPU for nothing. A frozen job
    is not cancelled here — the server refuses, and the error says what to do.
    """
    state_path = Path(meeting_dir) / STATE_FILE
    state = _load_json(state_path)
    if not state:
        return "no spark job recorded for this meeting / 这场会没有在等的服务器作业"
    job_dir = str(state.get("job_dir", ""))
    if _SAFE_REMOTE.match(job_dir):
        try:
            client.call("cancel", job_dir)
        except RemoteError as exc:
            if exc.data.get("error") != "bad_path":  # already gone is fine; frozen is not
                raise
        else:
            # The server only deletes a directory whose job has left the queue.
            waited = 0.0
            while waited < CANCEL_WAIT_SECONDS:
                with contextlib.suppress(RemoteError):
                    if client.call("status", job_dir).get("state") not in _ACTIVE:
                        break
                sleep(3.0)
                waited += 3.0
            _clean(client, job_dir)
    state_path.unlink(missing_ok=True)
    return f"cancelled spark job {state.get('job_id')} / 已撤回服务器作业 {state.get('job_id')}"


def transcribe_on_spark(
    meeting_dir: Path,
    files: list[tuple[str, Path]],
    *,
    vocab_context: str,
    chunk_minutes: float,
    client: SparkClient,
    reuse_result: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, list[Segment]]:
    """Transcribe `files` (role, wav) on spark; return {role: segments}.

    Blocks until the job finishes — through the queue, too. Interrupting is
    safe: the next call resumes the same job. With `reuse_result`, a result
    already fetched for exactly these inputs is used without asking spark.
    """
    meeting_dir = Path(meeting_dir)
    state_path = meeting_dir / STATE_FILE
    fp = fingerprint(files, vocab_context, chunk_minutes)

    cached = _load_json(meeting_dir / RESULT_FILE)
    if reuse_result and cached and cached.get("fingerprint") == fp:
        _log("[spark] 用已经取回的转录结果（同样的音频不重复上传）")
        return _segments(cached.get("result") or {})

    job_dir = None
    state = _load_json(state_path)
    if state and state.get("fingerprint") == fp:
        job_dir = _resume(state, client)
    elif state:
        # The audio, vocabulary or chunking changed since that job went in: its
        # answer is no use, so it should not hold the GPU or keep the old audio.
        _log("[spark] 输入变了，先撤掉上次的作业")
        try:
            cancel_on_spark(meeting_dir, client, sleep=sleep)
        except RemoteError as exc:
            _log(f"[spark] 上次的作业没撤掉（不影响这次）：{exc}")
    if job_dir is None:
        job_dir = _submit(meeting_dir, files, vocab_context, chunk_minutes, client, state_path, fp)

    status = _wait(job_dir, client, sleep)
    if not status.get("result_ready"):
        _fail(meeting_dir, job_dir, status, client, state_path)
    return _collect(meeting_dir, job_dir, client, state_path, fp)
