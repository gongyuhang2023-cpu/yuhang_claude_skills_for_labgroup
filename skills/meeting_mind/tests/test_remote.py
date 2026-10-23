"""Test remote — the client half of `--backend spark`, against a fake server."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf
import soxr

from meetingmind import remote
from meetingmind.remote import RemoteError, SparkClient, transcribe_on_spark
from meetingmind.transcribe import Segment

# --------------------------------------------------------------------- fixtures


def _write_wav(path: Path, seconds: float = 3.0, rate: int = 48_000, amp: float = 0.5) -> Path:
    t = np.arange(int(seconds * rate)) / rate
    tone = (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.stack([tone, tone * 0.5], axis=1), rate, subtype="PCM_16")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeSpark:
    """Duck-types SparkClient: remote paths map into `root`; status replies are scripted.

    Each entry of `statuses` is returned by one `status` call (the last one
    repeats). An entry with result_ready=True makes the fake 'finish' the job
    by writing result.json, the way `server.run` would.
    """

    def __init__(self, root: Path, statuses: list[Any], segments: dict | None = None) -> None:
        self.root = root
        self.statuses = list(statuses)
        self.segments = segments or {"main": [{"start": 0.0, "end": 180.0, "text": "你好", "speaker": None}]}
        self.calls: list[tuple[str, ...]] = []
        self.corrupt_uploads = 0
        self.jobs = 0
        #: command -> exceptions to raise on its next calls, in order
        self.fail_on: dict[str, list[Exception]] = {}

    def _path(self, remote_path: str) -> Path:
        return self.root / remote_path.lstrip("/")

    def call(self, *args: str, timeout: float = 60) -> dict[str, Any]:
        self.calls.append(args)
        cmd = args[0]
        if self.fail_on.get(cmd):
            raise self.fail_on[cmd].pop(0)
        if cmd == "cancel":
            return {"cancelled": True}
        if cmd == "new-job":
            self.jobs += 1
            d = f"/scratch/meetingmind/u/job{self.jobs}"
            self._path(d).mkdir(parents=True)
            return {"job_dir": d, "protocol": remote.PROTOCOL}
        if cmd == "submit":
            d = self._path(args[1])
            job = json.loads((d / "job.json").read_text(encoding="utf-8"))
            for entry in job["audio"]:
                if _sha(d / entry["file"]) != entry["sha256"]:
                    raise RemoteError("spark: damaged", {"error": "checksum", "file": entry["file"]})
            return {"job_id": "42", "time_limit": "00:20:00"}
        if cmd == "status":
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if isinstance(st, Exception):
                raise st
            if st.get("result_ready") and self._path(args[1]).is_dir():
                result = {"protocol": remote.PROTOCOL, "elapsed_seconds": 12.5, "segments": self.segments}
                (self._path(args[1]) / "result.json").write_text(json.dumps(result), encoding="utf-8")
            return {"job_id": "42", **st}
        if cmd == "clean":
            shutil.rmtree(self._path(args[1]), ignore_errors=True)
            return {"cleaned": True}
        raise AssertionError(f"unexpected call {args}")

    def upload(self, local: Path, remote_path: str) -> None:
        if self.fail_on.get("upload"):
            raise self.fail_on["upload"].pop(0)
        data = local.read_bytes()
        if self.corrupt_uploads and local.suffix == ".flac":
            self.corrupt_uploads -= 1
            data = data[:-7]
        self._path(remote_path).write_bytes(data)

    def download(self, remote_path: str, local: Path) -> None:
        src = self._path(remote_path)
        if not src.is_file():
            raise RemoteError("scp failed")
        local.write_bytes(src.read_bytes())

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def server_root(tmp_path: Path) -> Path:
    return tmp_path / "server"


@pytest.fixture
def meeting(meeting_dir: Path) -> Path:
    _write_wav(meeting_dir / "audio" / "recording.wav")
    return meeting_dir


def _run(meeting: Path, fake: FakeSpark, files: list[tuple[str, Path]] | None = None,
         sleeps: list[float] | None = None) -> dict[str, list[Segment]]:
    files = files or [("main", meeting / "audio" / "recording.wav")]
    return transcribe_on_spark(
        meeting, files, vocab_context="", chunk_minutes=3.0, client=fake,  # type: ignore[arg-type]
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


DONE = {"state": "COMPLETED", "result_ready": True}

# --------------------------------------------------------------------- audio


def test_prepare_flac_is_16k_mono_and_keeps_duration(tmp_path: Path) -> None:
    wav = _write_wav(tmp_path / "in.wav", seconds=3.0)
    out = tmp_path / "out.flac"
    seconds = remote.prepare_flac(wav, out)
    info = sf.info(str(out))
    assert (info.samplerate, info.channels, info.format) == (16_000, 1, "FLAC")
    assert seconds == pytest.approx(3.0, abs=0.01)
    assert info.duration == pytest.approx(3.0, abs=0.01)


def test_prepare_flac_clips_overshoot_instead_of_wrapping(tmp_path: Path) -> None:
    """Resampling a full-scale square wave overshoots ±1.0 (Gibbs).

    Encoded without clipping, those samples would wrap to the opposite sign —
    audible clicks the model then tries to transcribe.
    """
    rate = 48_000
    square = np.where((np.arange(rate * 2) // (rate // 4)) % 2 == 0, 0.99, -0.99).astype(np.float32)
    wav = tmp_path / "square.wav"
    sf.write(str(wav), np.stack([square, square], axis=1), rate, subtype="FLOAT")
    stream = soxr.ResampleStream(rate, 16_000, 1, dtype="float32", quality="HQ")
    expected = np.concatenate([stream.resample_chunk(square),
                               stream.resample_chunk(np.zeros(0, np.float32), last=True)])
    assert expected.max() > 1.0, "test signal must actually overshoot"

    out = tmp_path / "square.flac"
    remote.prepare_flac(wav, out)
    got, _ = sf.read(str(out), dtype="float32")
    assert len(got) == len(expected)
    assert np.abs(got - np.clip(expected, -1.0, 1.0)).max() < 3 / 32768


def test_fingerprint_follows_audio_vocab_and_chunking(tmp_path: Path) -> None:
    wav = _write_wav(tmp_path / "a.wav")
    files = [("main", wav)]
    base = remote.fingerprint(files, "", 3.0)
    assert remote.fingerprint(files, "", 3.0) == base
    assert remote.fingerprint(files, "术语", 3.0) != base
    assert remote.fingerprint(files, "", 2.0) != base
    _write_wav(wav, seconds=4.0)
    assert remote.fingerprint(files, "", 3.0) != base


# --------------------------------------------------------------------- SparkClient


class Recorder:
    def __init__(self, returncode: int = 0, stdout: str = "{}", stderr: str = "",
                 exc: Exception | None = None) -> None:
        self.argv: list[str] = []
        self.kwargs: dict[str, Any] = {}
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.exc = exc

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.argv, self.kwargs = argv, kwargs
        if self.exc:
            raise self.exc
        return self.result


def test_call_quotes_every_argument_for_the_remote_shell() -> None:
    rec = Recorder(stdout='{"job_dir": "/scratch/x"}\n')
    client = SparkClient("spark", "/opt/bio/apps/bin/meetingmind-asr", runner=rec)
    assert client.call("new-job", "lab meeting; rm -rf ~") == {"job_dir": "/scratch/x"}
    assert rec.argv[0] == "ssh" and "BatchMode=yes" in rec.argv
    assert rec.argv[-2] == "spark"
    assert shlex.split(rec.argv[-1]) == [
        "/opt/bio/apps/bin/meetingmind-asr", "new-job", "lab meeting; rm -rf ~",
    ]


def test_unreachable_host_explains_what_to_check() -> None:
    rec = Recorder(returncode=255, stdout="", stderr="ssh: connect to host spark: timed out")
    with pytest.raises(RemoteError, match="Tailscale"):
        SparkClient("spark", "cmd", runner=rec).call("queue")


def test_missing_server_command_says_not_deployed() -> None:
    """ssh worked; the command is absent. Sending people to check their network would mislead."""
    rec = Recorder(returncode=127, stdout="", stderr="bash: meetingmind-asr: No such file or directory")
    with pytest.raises(RemoteError) as excinfo:
        SparkClient("spark", "/opt/bio/apps/bin/meetingmind-asr", runner=rec).call("queue")
    assert excinfo.value.data["error"] == "not_deployed"
    assert "Tailscale" not in str(excinfo.value)


def test_server_refusal_keeps_its_json() -> None:
    rec = Recorder(returncode=4, stdout='{"error": "checksum", "file": "main.flac", "message": "damaged"}')
    with pytest.raises(RemoteError) as excinfo:
        SparkClient("spark", "cmd", runner=rec).call("submit", "/scratch/x")
    assert excinfo.value.data["error"] == "checksum"
    assert "damaged" in str(excinfo.value)


def test_missing_ssh_binary_is_a_remote_error() -> None:
    rec = Recorder(exc=FileNotFoundError("ssh"))
    with pytest.raises(RemoteError, match="OpenSSH"):
        SparkClient("spark", "cmd", runner=rec).call("queue")


def test_scp_uses_a_bare_file_name_inside_its_folder(tmp_path: Path) -> None:
    """`C:\\...` would be parsed by scp as host 'C' — hence cwd + bare name."""
    rec = Recorder()
    local = tmp_path / "main.flac"
    SparkClient("spark", "cmd", runner=rec).upload(local, "/scratch/meetingmind/u/j/main.flac")
    assert rec.argv[0] == "scp"
    assert rec.argv[-2:] == ["main.flac", "spark:/scratch/meetingmind/u/j/main.flac"]
    assert rec.kwargs["cwd"] == str(tmp_path)


def test_odd_remote_paths_are_refused_before_running_anything(tmp_path: Path) -> None:
    rec = Recorder()
    with pytest.raises(RemoteError, match="unexpected"):
        SparkClient("spark", "cmd", runner=rec).download("/x/$(reboot)", tmp_path / "r.json")
    assert rec.argv == []


# --------------------------------------------------------------------- lifecycle


def test_happy_path_queue_then_run_then_result(meeting: Path, server_root: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeSpark(server_root, [
        {"state": "PENDING", "reason": "Resources", "ahead": 1},
        {"state": "RUNNING", "progress": "1/2", "elapsed": "0:20"},
        DONE,
    ])
    sleeps: list[float] = []
    out = _run(meeting, fake, sleeps=sleeps)

    assert out == {"main": [Segment(0.0, 180.0, "你好", None)]}
    assert fake.commands() == ["new-job", "submit", "status", "status", "status", "clean"]
    assert sleeps == [remote.POLL_PENDING, remote.POLL_RUNNING]
    assert not (meeting / remote.STATE_FILE).exists(), "state must go once the result is in"
    assert not (server_root / "scratch/meetingmind/u/job1").exists(), "server copy must be cleaned"
    saved = json.loads((meeting / remote.RESULT_FILE).read_text(encoding="utf-8"))
    assert saved["result"]["segments"]["main"][0]["text"] == "你好"
    err = capsys.readouterr().err
    assert "前面约有 1 个 GPU 作业" in err and "第 1/2 块" in err


def test_job_json_lists_both_streams_with_checksums(meeting: Path, server_root: Path) -> None:
    mic = _write_wav(meeting / "audio" / "mic.wav", seconds=2.0)
    seen: dict[str, Any] = {}

    class Peek(FakeSpark):
        def call(self, *args: str, timeout: float = 60) -> dict[str, Any]:
            if args[0] == "submit":
                seen.update(json.loads((self._path(args[1]) / "job.json").read_text(encoding="utf-8")))
            return super().call(*args, timeout=timeout)

    fake = Peek(server_root, [DONE], segments={"main": [], "mic": []})
    _run(meeting, fake, files=[("main", meeting / "audio" / "recording.wav"), ("mic", mic)])
    assert [a["role"] for a in seen["audio"]] == ["main", "mic"]
    assert [a["file"] for a in seen["audio"]] == ["main.flac", "mic.flac"]
    assert seen["audio"][1]["seconds"] == pytest.approx(2.0, abs=0.01)
    assert seen["protocol"] == remote.PROTOCOL


def test_damaged_upload_is_sent_again(meeting: Path, server_root: Path) -> None:
    fake = FakeSpark(server_root, [DONE])
    fake.corrupt_uploads = 1
    _run(meeting, fake)
    assert fake.commands().count("submit") == 2
    assert fake.commands().count("new-job") == 1


def test_rerun_resumes_the_same_job_instead_of_submitting_again(meeting: Path, server_root: Path) -> None:
    """A closed terminal or lost session must not cost a second GPU job."""
    files = [("main", meeting / "audio" / "recording.wav")]
    job_dir = "/scratch/meetingmind/u/job7"
    (server_root / job_dir.lstrip("/")).mkdir(parents=True)
    state = {"job_dir": job_dir, "job_id": "42",
             "fingerprint": remote.fingerprint(files, "", 3.0)}
    (meeting / "transcript").mkdir()
    (meeting / remote.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")

    fake = FakeSpark(server_root, [{"state": "RUNNING", "progress": "2/3"}, {"state": "RUNNING"}, DONE])
    out = _run(meeting, fake, files=files)
    assert "new-job" not in fake.commands()
    assert out["main"][0].text == "你好"


def test_rerun_after_a_failed_job_submits_afresh(meeting: Path, server_root: Path) -> None:
    files = [("main", meeting / "audio" / "recording.wav")]
    (server_root / "scratch/meetingmind/u/old").mkdir(parents=True)
    state = {"job_dir": "/scratch/meetingmind/u/old", "fingerprint": remote.fingerprint(files, "", 3.0)}
    (meeting / "transcript").mkdir()
    (meeting / remote.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")

    fake = FakeSpark(server_root, [{"state": "FAILED"}, DONE])
    _run(meeting, fake, files=files)
    assert fake.commands()[:3] == ["status", "clean", "new-job"]


def test_changed_audio_withdraws_the_old_job_and_submits_anew(meeting: Path, server_root: Path) -> None:
    """The old job's answer is for other inputs: it must not hold the GPU or keep its audio."""
    (server_root / "scratch/meetingmind/u/old").mkdir(parents=True)
    (meeting / "transcript").mkdir()
    (meeting / remote.STATE_FILE).write_text(
        json.dumps({"job_dir": "/scratch/meetingmind/u/old", "fingerprint": "stale"}), encoding="utf-8")
    fake = FakeSpark(server_root, [DONE])
    _run(meeting, fake)
    assert fake.commands()[:4] == ["cancel", "status", "clean", "new-job"]
    assert not (server_root / "scratch/meetingmind/u/old").exists()


def test_failed_job_brings_back_the_partial_transcript(meeting: Path, server_root: Path) -> None:
    class Partial(FakeSpark):
        def call(self, *args: str, timeout: float = 60) -> dict[str, Any]:
            if args[0] == "status":
                (self._path(args[1]) / "partial_main.md").write_text("[00:00:00] 前半段", encoding="utf-8")
            return super().call(*args, timeout=timeout)

    fake = Partial(server_root, [{"state": "TIMEOUT", "log_tail": "chunk 9/20"}])
    with pytest.raises(RuntimeError, match="TIMEOUT") as excinfo:
        _run(meeting, fake)
    assert "chunk 9/20" in str(excinfo.value)
    assert (meeting / "transcript" / "_partial.md").read_text(encoding="utf-8") == "[00:00:00] 前半段"
    assert not (meeting / remote.STATE_FILE).exists()
    assert fake.commands()[-1] == "clean"


def test_frozen_job_explains_the_safe_cancel_and_keeps_state(meeting: Path, server_root: Path) -> None:
    fake = FakeSpark(server_root, [{"state": "FROZEN"}])
    with pytest.raises(RuntimeError, match="cancel-frozen"):
        _run(meeting, fake)
    assert (meeting / remote.STATE_FILE).exists()
    assert "clean" not in fake.commands()


def test_network_blips_while_waiting_are_tolerated(meeting: Path, server_root: Path) -> None:
    blip = RemoteError("Cannot reach spark over ssh")
    fake = FakeSpark(server_root, [blip, blip, {"state": "RUNNING"}, DONE])
    out = _run(meeting, fake)
    assert out["main"]


def _pending_state(meeting: Path, server_root: Path) -> None:
    (server_root / "scratch/meetingmind/u/job9").mkdir(parents=True)
    (meeting / "transcript").mkdir()
    (meeting / remote.STATE_FILE).write_text(
        json.dumps({"job_dir": "/scratch/meetingmind/u/job9", "job_id": "77"}), encoding="utf-8")


def test_spark_cancel_withdraws_then_cleans_once_the_job_is_gone(meeting: Path,
                                                                 server_root: Path) -> None:
    _pending_state(meeting, server_root)
    fake = FakeSpark(server_root, [{"state": "COMPLETING"}, {"state": "CANCELLED"}])
    sleeps: list[float] = []
    assert "77" in remote.cancel_on_spark(meeting, fake, sleep=sleeps.append)  # type: ignore[arg-type]
    assert fake.commands() == ["cancel", "status", "status", "clean"]
    assert len(sleeps) == 1
    assert not (meeting / remote.STATE_FILE).exists()


def test_spark_cancel_surfaces_a_frozen_job(meeting: Path, server_root: Path) -> None:
    _pending_state(meeting, server_root)

    class Frozen(FakeSpark):
        def call(self, *args: str, timeout: float = 60) -> dict[str, Any]:
            if args[0] == "cancel":
                raise RemoteError("spark: frozen; run advise.py cancel-frozen 77", {"error": "frozen"})
            return super().call(*args, timeout=timeout)

    with pytest.raises(RemoteError, match="cancel-frozen"):
        remote.cancel_on_spark(meeting, Frozen(server_root, [DONE]))  # type: ignore[arg-type]
    assert (meeting / remote.STATE_FILE).exists()


def test_spark_cancel_without_a_job_says_so(meeting: Path, server_root: Path) -> None:
    assert "没有" in remote.cancel_on_spark(meeting, FakeSpark(server_root, [DONE]))  # type: ignore[arg-type]


def test_fetched_result_survives_a_crash_and_is_reused(meeting: Path, server_root: Path) -> None:
    """Saved before the server copy goes; a rerun needs no server at all."""
    _run(meeting, FakeSpark(server_root, [DONE]))
    again = FakeSpark(server_root, [DONE])
    out = _run(meeting, again)
    assert again.calls == []
    assert out["main"][0].text == "你好"


def test_force_transcribes_again_instead_of_reusing(meeting: Path, server_root: Path) -> None:
    _run(meeting, FakeSpark(server_root, [DONE]))
    again = FakeSpark(server_root, [DONE])
    transcribe_on_spark(meeting, [("main", meeting / "audio" / "recording.wav")], vocab_context="",
                        chunk_minutes=3.0, client=again, reuse_result=False,  # type: ignore[arg-type]
                        sleep=lambda _s: None)
    assert "new-job" in again.commands()


def test_submit_with_no_reply_keeps_state_and_deletes_nothing(meeting: Path, server_root: Path) -> None:
    """sbatch may have run before the connection dropped: cleaning could delete a queued job."""
    fake = FakeSpark(server_root, [DONE])
    fake.fail_on["submit"] = [RemoteError("spark did not answer within 60 s")]
    with pytest.raises(RemoteError, match="unknown"):
        _run(meeting, fake)
    assert "clean" not in fake.commands()
    state = json.loads((meeting / remote.STATE_FILE).read_text(encoding="utf-8"))
    assert state["phase"] == "submitting"
    assert (server_root / state["job_dir"].lstrip("/")).is_dir()


def test_rerun_after_an_unanswered_submit_finds_the_queued_job(meeting: Path, server_root: Path) -> None:
    fake = FakeSpark(server_root, [{"state": "RUNNING"}, DONE])
    fake.fail_on["submit"] = [RemoteError("spark did not answer within 60 s")]
    with pytest.raises(RemoteError):
        _run(meeting, fake)
    out = _run(meeting, fake)
    assert fake.commands().count("new-job") == 1, "the queued job is picked up, not doubled"
    assert out["main"][0].text == "你好"


def test_rerun_after_an_unanswered_submit_that_never_queued(meeting: Path, server_root: Path) -> None:
    fake = FakeSpark(server_root, [{"state": "NOT_SUBMITTED"}, DONE])
    fake.fail_on["submit"] = [RemoteError("spark did not answer within 60 s")]
    with pytest.raises(RemoteError):
        _run(meeting, fake)
    _run(meeting, fake)
    cmds = fake.commands()
    assert cmds[cmds.index("status"):][:3] == ["status", "clean", "new-job"]


def test_failed_upload_takes_the_partial_upload_down(meeting: Path, server_root: Path) -> None:
    fake = FakeSpark(server_root, [DONE])
    fake.fail_on["upload"] = [RemoteError("scp failed: broken pipe")]
    with pytest.raises(RemoteError, match="scp"):
        _run(meeting, fake)
    assert fake.commands() == ["new-job", "clean"]
    assert not (meeting / remote.STATE_FILE).exists()
    assert not (server_root / "scratch/meetingmind/u/job1").exists()


def test_outright_refusal_cleans_up(meeting: Path, server_root: Path) -> None:
    fake = FakeSpark(server_root, [DONE])
    fake.fail_on["submit"] = [RemoteError("spark: sbatch refused", {"error": "sbatch"})]
    with pytest.raises(RemoteError, match="sbatch"):
        _run(meeting, fake)
    assert fake.commands()[-1] == "clean"
    assert not (meeting / remote.STATE_FILE).exists()


def test_unknown_state_is_left_alone(meeting: Path, server_root: Path) -> None:
    """No final word from Slurm: deleting the directory could delete a live job."""
    fake = FakeSpark(server_root, [{"state": "UNKNOWN"}])
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        _run(meeting, fake)
    assert "clean" not in fake.commands()
    assert (meeting / remote.STATE_FILE).exists()


def test_resume_with_unknown_state_touches_nothing(meeting: Path, server_root: Path) -> None:
    files = [("main", meeting / "audio" / "recording.wav")]
    (server_root / "scratch/meetingmind/u/job7").mkdir(parents=True)
    (meeting / "transcript").mkdir()
    (meeting / remote.STATE_FILE).write_text(json.dumps({
        "job_dir": "/scratch/meetingmind/u/job7", "fingerprint": remote.fingerprint(files, "", 3.0),
    }), encoding="utf-8")
    fake = FakeSpark(server_root, [{"state": "UNKNOWN"}])
    with pytest.raises(RemoteError, match="spark-cancel"):
        _run(meeting, fake, files=files)
    assert fake.commands() == ["status"]


def test_slurm_hiccups_while_waiting_are_retried(meeting: Path, server_root: Path) -> None:
    down = RemoteError("spark: squeue failed", {"error": "slurm_unavailable"})
    fake = FakeSpark(server_root, [down, down, DONE])
    assert _run(meeting, fake)["main"]


def test_server_with_another_protocol_is_refused(meeting: Path, server_root: Path) -> None:
    class Old(FakeSpark):
        def call(self, *args: str, timeout: float = 60) -> dict[str, Any]:
            reply = super().call(*args, timeout=timeout)
            return {**reply, "protocol": 0} if args[0] == "new-job" else reply

    with pytest.raises(RemoteError, match="protocol"):
        _run(meeting, Old(server_root, [DONE]))
