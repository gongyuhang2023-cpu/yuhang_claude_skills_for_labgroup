"""Server side of `--backend spark`: what `meetingmind-asr` runs on the lab server.

The client (`remote.py`) and this module talk through one job directory and
through a single JSON line each subcommand prints on stdout:

    /scratch/meetingmind/<user>/<name>-<stamp>-<rand>/
        job.json           client: audio list (sha256, seconds), options
        main.flac          client: the meeting audio (mic.flac too, two-stream)
        job.sbatch         `submit`
        slurm_job_id       `submit`
        slurm.log          the job's output, incl. "[transcribe] chunk i/n"
        tmp/               the job's TMPDIR: per-chunk WAVs live here, not in /tmp
        partial_<role>.md  the Transcriber, one paragraph per finished chunk
        result.json        `run`, atomically, once every file is transcribed

Every subcommand except `run` uses the standard library only and returns in
well under a second, because they run on the login node, where heavy work is
not allowed. `run` is what the Slurm job executes, on the GPU.

/scratch is excluded from the nightly backup, and that is the point: meeting
audio must not end up in it. **The audio never outlives the job**: `run`
deletes it in a `finally`, and the job script's EXIT/TERM trap deletes it
again (with the chunk WAVs in tmp/) when Slurm kills the job at its time
limit or on scancel. What remains — log, partial and final text — goes when
the client `clean`s the directory after fetching the result, or when
`new-job` prunes the caller's directories older than a week.

Everything runs as the calling user. The parent directory is 1777 (sticky) and
each user's subdirectory 0700, so nobody can read or remove another person's
files; the path checks below stop accidents (a `..`, a symlink), they are not
the security boundary.

**When Slurm cannot be asked, nothing is decided.** squeue/sacct failing is
reported as `slurm_unavailable`, never read as "the job is gone": that reading
would let a status poll during a slurmctld hiccup delete a running job's
directory, or let `cancel` scancel a job whose frozen state it could not see.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__

#: Bumped whenever job.json / result.json / the stdout JSON change shape.
PROTOCOL = 1

SCRATCH_ROOT = Path(os.environ.get("MEETINGMIND_SCRATCH", "/scratch/meetingmind"))
MODEL_DIR = Path(os.environ.get("MEETINGMIND_MODEL", "/opt/bio/db/qwen3-asr-1.7b/current"))
#: Lab-specific settings handed to clients (the cloud's address and meetings
#: folder). Kept on the server, not in the skill: the skill is published.
LAB_FILE = Path(os.environ.get("MEETINGMIND_LAB", "/opt/bio/apps/meetingmind/lab.json"))
STALE_DAYS = 7

# Slurm request for one transcription job. GPU jobs run one at a time on this
# machine, so the GPU is always the whole GPU; --mem is a *declaration* that
# spark-memguard compares against real use, CUDA memory included.
#
# Measured 2026-10-23 (job 1316, a 33-minute meeting, 3-minute chunks, fp16 on
# the GB10): 2 min 55 s wall, model load 10 s, i.e. ~5 s per minute of audio;
# one CPU core busy; host RSS peak 6.5 GB plus CUDA 5.5 GB allocated / 7.2 GB
# reserved. --mem is that ~13.7 GB peak with 1.5x headroom; under-declaring
# would make this the first job memguard freezes. Per chunk the memory is
# flat; what grows with length is the decoded audio (16 kHz float32, ~0.23 GB
# per hour), so job.json is held to the range that budget covers.
JOB_CPUS = 2
JOB_MEM = "20G"
MAX_CHUNK_MINUTES = 5.0
MAX_AUDIO_SECONDS = 8 * 3600
#: --time = TIME_FIXED_MIN + TIME_PER_AUDIO_MIN × audio minutes, at least TIME_MIN
#: (about three times the measured speed).
TIME_FIXED_MIN = 10
TIME_PER_AUDIO_MIN = 0.25
TIME_MIN = 15

ROLES = ("main", "mic")
_ACTIVE = frozenset({"PENDING", "CONFIGURING", "RUNNING", "COMPLETING", "SUSPENDED", "REQUEUED"})
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_JOB_NAME = re.compile(r"^[a-z0-9-]{1,40}-\d{8}-\d{6}-[0-9a-f]{4}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_LINE = re.compile(r"\[transcribe\] chunk (\d+)/(\d+)")


class JobError(Exception):
    """A refusal the client should show as is. `code` is machine-readable."""

    def __init__(self, code: str, message: str, exit_code: int = 2, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.extra = extra


# --------------------------------------------------------------------- helpers


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run a Slurm command. Module-level so tests can replace it."""
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return cp.returncode, cp.stdout, cp.stderr


def _slurm_unavailable(cmd: str, err: str) -> JobError:
    return JobError(
        "slurm_unavailable",
        f"{cmd} failed, so the job's state is unknown — nothing was changed: {err.strip()[-300:]}",
        exit_code=7,
    )


def _user_root() -> Path:
    return SCRATCH_ROOT / getpass.getuser()


def slugify(name: str) -> str:
    """Directory-safe, lowercase, at most 40 characters; 'meeting' if nothing is left."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug or "meeting"


def is_job_path(arg: str, root: str) -> bool:
    """Is `arg`, as written, a job directory directly under `root`?

    Lexical on purpose: `<root>/..` has `<root>` as its lexical parent, so the
    `..` must be refused by name, not by comparison — and on the raw string,
    since PurePosixPath silently drops `.` segments.
    """
    if not _SAFE_PATH.match(arg) or any(seg in (".", "..") for seg in arg.split("/")):
        return False
    path = PurePosixPath(arg)
    return path.parent == PurePosixPath(root) and bool(_JOB_NAME.match(path.name))


def _job_dir(arg: str) -> Path:
    """Accept only one of the caller's own job directories — no `..`, no symlink."""
    root = _user_root()
    d = Path(arg)
    if (not is_job_path(arg, root.as_posix()) or d.is_symlink() or not d.is_dir()
            or d.resolve().parent != root.resolve()):
        raise JobError("bad_path", f"not one of your job directories: {arg}")
    return d


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_job(d: Path) -> dict[str, Any]:
    """Load and validate job.json. Anything malformed is refused, not guessed at."""
    try:
        job = json.loads((d / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JobError("bad_job", f"cannot read job.json: {exc}") from exc
    if job.get("protocol") != PROTOCOL:
        raise JobError(
            "protocol",
            f"client speaks protocol {job.get('protocol')!r}, server {PROTOCOL}: "
            "update the skill (or ask an admin to update the server copy)",
            exit_code=3,
            server_protocol=PROTOCOL,
        )
    audio = job.get("audio")
    if not isinstance(audio, list) or not audio:
        raise JobError("bad_job", "job.json lists no audio")
    seen: set[str] = set()
    for entry in audio:
        role, name = entry.get("role"), entry.get("file")
        if role not in ROLES or role in seen:
            raise JobError("bad_job", f"bad or repeated audio role: {role!r}")
        seen.add(role)
        if name != f"{role}.flac":
            raise JobError("bad_job", f"audio file must be named {role}.flac, got {name!r}")
        if not _SHA256.match(str(entry.get("sha256", ""))):
            raise JobError("bad_job", f"{name}: missing sha256")
        seconds = entry.get("seconds")
        if not isinstance(seconds, (int, float)) or not 0 < seconds <= MAX_AUDIO_SECONDS:
            raise JobError("bad_job", f"{name}: duration {seconds!r} s outside (0, {MAX_AUDIO_SECONDS}]")
    chunk = job.get("chunk_minutes", 3.0)
    if not isinstance(chunk, (int, float)) or not 0.5 <= chunk <= MAX_CHUNK_MINUTES:
        raise JobError("bad_job", f"chunk_minutes {chunk!r} outside [0.5, {MAX_CHUNK_MINUTES}]")
    if not isinstance(job.get("vocab_context", ""), str):
        raise JobError("bad_job", "vocab_context must be a string")
    return job


def time_limit_minutes(audio_seconds: float) -> int:
    """Slurm --time for this much audio (all files together)."""
    est = TIME_FIXED_MIN + TIME_PER_AUDIO_MIN * audio_seconds / 60.0
    return max(TIME_MIN, math.ceil(est))


def _hms(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def sbatch_script(d: Path, minutes: int) -> str:
    job = d.as_posix()
    src = Path(__file__).resolve().parents[1].as_posix()
    audio = " ".join(f'"$D/{role}.flac"' for role in ROLES)
    return "\n".join([
        "#!/bin/bash -l",
        "#SBATCH -J mm-asr",
        "#SBATCH --gres=gpu:1",
        f"#SBATCH -c {JOB_CPUS}",
        f"#SBATCH --mem={JOB_MEM}",
        f"#SBATCH -t {_hms(minutes)}",
        f"#SBATCH -D {job}",
        f"#SBATCH -o {job}/slurm.log",
        "set -uo pipefail",
        f"D={shlex.quote(job)}",
        "# The audio and the chunk WAVs never outlive the job: this also runs when",
        "# Slurm sends TERM at the time limit or on scancel (KillWait before KILL).",
        f'cleanup() {{ rm -rf -- "$D/tmp" {audio}; }}',
        "trap cleanup EXIT",
        "trap 'exit 143' TERM INT",
        'mkdir -p "$D/tmp"',
        'export TMPDIR="$D/tmp"',
        "export HF_HUB_OFFLINE=1",
        f"export MEETINGMIND_SCRATCH={shlex.quote(SCRATCH_ROOT.as_posix())}",
        f"export MEETINGMIND_MODEL={shlex.quote(MODEL_DIR.as_posix())}",
        f"export PYTHONPATH={shlex.quote(src)}",
        f'{shlex.quote(sys.executable)} -m meetingmind.server run "$D"',
        "",
    ])


def _slurm_id(d: Path) -> str | None:
    with contextlib.suppress(OSError):
        jid = (d / "slurm_job_id").read_text(encoding="utf-8").strip()
        return jid if jid.isdigit() else None
    return None


def _squeue_state(jid: str) -> tuple[str, str, str, str] | None:
    """(state, reason, elapsed, limit) while Slurm still lists the job, else None.

    Finished jobs stay listed (in their final state) for MinJobAge, 300 s here.
    """
    rc, out, err = _run(["squeue", "-h", "-j", jid, "-o", "%T|%r|%M|%l"])
    if rc != 0:
        if "Invalid job id" in err:  # squeue's way of saying "not in the queue"
            return None
        raise _slurm_unavailable("squeue", err)
    line = out.strip().splitlines()[0] if out.strip() else ""
    parts = line.split("|")
    return (parts[0], parts[1], parts[2], parts[3]) if len(parts) == 4 else None


def _sacct_state(jid: str) -> str:
    """The job's final state from accounting; "" when accounting has no record."""
    rc, out, err = _run(["sacct", "-j", jid, "-X", "-n", "-P", "-o", "State"])
    if rc != 0:
        raise _slurm_unavailable("sacct", err)
    first = out.strip().splitlines()[0] if out.strip() else ""
    return first.split()[0] if first else ""  # "CANCELLED by 1001" -> "CANCELLED"


def _frozen(jid: str) -> bool:
    """spark-memguard parks a placeholder job named after the one it froze."""
    rc, out, err = _run(["squeue", "-h", "-n", f"memguard-hold-{jid}", "-o", "%A"])
    if rc != 0:
        raise _slurm_unavailable("squeue", err)
    return bool(out.strip())


def _gpu_jobs() -> list[dict[str, Any]]:
    """Every queued or running job that asks for the GPU."""
    rc, out, err = _run(["squeue", "-h", "-o", "%A|%T|%b|%Q|%M|%l"])
    if rc != 0:
        raise _slurm_unavailable("squeue", err)
    jobs = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 6 or "gpu" not in parts[2]:
            continue
        try:
            jid, prio = int(parts[0]), int(parts[3])
        except ValueError:
            continue
        jobs.append({"id": jid, "state": parts[1], "priority": prio,
                     "elapsed": parts[4], "limit": parts[5]})
    return jobs


def gpu_jobs_ahead(jid: str, jobs: list[dict[str, Any]]) -> int:
    """GPU jobs that will run before `jid`: whatever holds the GPU, plus pending
    jobs Slurm ranks higher. An estimate — backfill and fairshare can reorder."""
    me = next((j for j in jobs if j["id"] == int(jid)), None)
    ahead = sum(1 for j in jobs if j["state"] == "RUNNING" and j is not me)
    if me is None:
        return ahead + sum(1 for j in jobs if j["state"] == "PENDING")
    return ahead + sum(
        1 for j in jobs
        if j["state"] == "PENDING" and j is not me
        and (j["priority"], -j["id"]) > (me["priority"], -me["id"])
    )


def progress_of(log: Path) -> str | None:
    """The last 'chunk i/n' the Transcriber reported, as 'i/n'."""
    with contextlib.suppress(OSError):
        matches = _CHUNK_LINE.findall(log.read_text(encoding="utf-8", errors="replace"))
        if matches:
            return f"{matches[-1][0]}/{matches[-1][1]}"
    return None


def _log_tail(log: Path, lines: int = 15) -> str:
    with contextlib.suppress(OSError):
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    return ""


def _prune(root: Path) -> list[str]:
    """Remove the caller's job directories untouched for STALE_DAYS whose job is gone.

    Opportunistic: it runs when the user starts a new job. If Slurm cannot be
    asked, nothing is removed this time.
    """
    cutoff = time.time() - STALE_DAYS * 86400
    removed = []
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            if child.is_symlink() or not child.is_dir() or child.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        jid = _slurm_id(child)
        try:
            if jid and _squeue_state(jid) is not None:
                continue
        except JobError:
            break
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child.name)
    return removed


def _drop_audio(d: Path) -> None:
    for role in ROLES:
        (d / f"{role}.flac").unlink(missing_ok=True)
    shutil.rmtree(d / "tmp", ignore_errors=True)


# --------------------------------------------------------------------- commands


def cmd_new_job(name: str) -> dict[str, Any]:
    if not SCRATCH_ROOT.is_dir():
        raise JobError(
            "not_deployed",
            f"{SCRATCH_ROOT} does not exist on this server — server-side transcription "
            "is not set up here; ask an admin",
        )
    root = _user_root()
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    pruned = _prune(root)
    d = root / f"{slugify(name)}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return {"job_dir": d.as_posix(), "protocol": PROTOCOL, "pruned": pruned}


def cmd_submit(arg: str) -> dict[str, Any]:
    d = _job_dir(arg)
    job = read_job(d)
    jid = _slurm_id(d)
    if jid:
        queued = _squeue_state(jid)
        if queued is not None and queued[0] in _ACTIVE:
            return {"job_id": jid, "already_submitted": True}
    for entry in job["audio"]:
        path = d / entry["file"]
        if not path.is_file() or sha256_of(path) != entry["sha256"]:
            raise JobError("checksum", f"{entry['file']} is missing or damaged", exit_code=4,
                           file=entry["file"])

    minutes = time_limit_minutes(sum(e["seconds"] for e in job["audio"]))
    script = d / "job.sbatch"
    script.write_text(sbatch_script(d, minutes), encoding="utf-8")
    rc, out, err = _run(["sbatch", "--parsable", str(script)])
    jid = out.strip().split(";")[0] if rc == 0 else ""
    if not jid.isdigit():
        raise JobError("sbatch", f"sbatch refused the job: {err.strip() or out.strip()}", exit_code=5)
    (d / "slurm_job_id").write_text(jid, encoding="utf-8")
    return {"job_id": jid, "time_limit": _hms(minutes)}


def cmd_status(arg: str) -> dict[str, Any]:
    d = _job_dir(arg)
    jid = _slurm_id(d)
    info: dict[str, Any] = {"job_id": jid, "result_ready": (d / "result.json").is_file()}
    if jid is None:
        info["state"] = "NOT_SUBMITTED"
        return info
    queued = _squeue_state(jid)
    if queued is not None:
        state, reason, elapsed, limit = queued
        info.update(state=state, reason=reason, elapsed=elapsed, limit=limit)
        if state in _ACTIVE and _frozen(jid):
            info["state"] = "FROZEN"
        elif state == "PENDING":
            with contextlib.suppress(JobError):  # the position is a nicety, not a decision
                info["ahead"] = gpu_jobs_ahead(jid, _gpu_jobs())
    else:
        # Gone from the queue: accounting has the final state. No record at all
        # (rare) is UNKNOWN, which the client treats as "do not clean up".
        info["state"] = _sacct_state(jid) or ("COMPLETED" if info["result_ready"] else "UNKNOWN")
    progress = progress_of(d / "slurm.log")
    if progress:
        info["progress"] = progress
    if info["state"] not in _ACTIVE and info["state"] != "FROZEN" and not info["result_ready"]:
        info["log_tail"] = _log_tail(d / "slurm.log")
    return info


def cmd_cancel(arg: str) -> dict[str, Any]:
    d = _job_dir(arg)
    jid = _slurm_id(d)
    if jid is None:
        return {"cancelled": False, "reason": "not submitted"}
    if _frozen(jid):  # raises slurm_unavailable rather than guess "not frozen"
        # A frozen job cannot receive signals: scancel leaves it stuck in
        # COMPLETING with its memory held (this machine, 2026-08-27).
        raise JobError(
            "frozen",
            f"job {jid} is frozen by spark-memguard; plain scancel would leave it stuck. "
            f"Run: advise.py cancel-frozen {jid}  (or ask an admin)",
            exit_code=6,
        )
    rc, _, err = _run(["scancel", jid])
    if rc != 0 and "Invalid job id" not in err:
        raise JobError("scancel", f"scancel {jid} failed: {err.strip()}")
    return {"cancelled": rc == 0, "job_id": jid}


def cmd_queue() -> dict[str, Any]:
    gpu = _gpu_jobs()
    return {
        "gpu_running": [
            {"elapsed": j["elapsed"], "limit": j["limit"]} for j in gpu if j["state"] == "RUNNING"
        ],
        "gpu_pending": sum(1 for j in gpu if j["state"] == "PENDING"),
    }


def cmd_clean(arg: str) -> dict[str, Any]:
    d = _job_dir(arg)
    jid = _slurm_id(d)
    if jid:
        queued = _squeue_state(jid)
        if queued is not None and queued[0] in _ACTIVE:
            raise JobError("active", f"job {jid} is still {queued[0]}; cancel it first", exit_code=8)
    shutil.rmtree(d)
    return {"cleaned": True}


def cmd_lab() -> dict[str, Any]:
    """The lab's cloud address and meetings folder, for `meetingmind cloud login`."""
    try:
        data = json.loads(LAB_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JobError("not_configured", f"{LAB_FILE} is missing or unreadable — ask an admin: {exc}") from exc
    url, folder = data.get("cloud_url"), data.get("cloud_folder")
    if not (isinstance(url, str) and url.startswith("https://")
            and isinstance(folder, str) and folder.startswith("/")):
        raise JobError("not_configured", f"{LAB_FILE} lacks cloud_url / cloud_folder — ask an admin")
    return {"cloud_url": url.rstrip("/"), "cloud_folder": folder.rstrip("/")}


def cmd_doctor() -> dict[str, Any]:
    model_ok = (MODEL_DIR / "config.json").is_file() and any(MODEL_DIR.glob("*.safetensors"))
    scratch_ok = SCRATCH_ROOT.is_dir() and os.access(SCRATCH_ROOT, os.W_OK | os.X_OK)
    stack_ok = all(importlib.util.find_spec(m) for m in ("torch", "qwen_asr", "librosa"))
    return {
        "ok": model_ok and scratch_ok and stack_ok,
        "protocol": PROTOCOL,
        "version": __version__,
        "model_dir": str(MODEL_DIR),
        "model_ok": model_ok,
        "scratch_ok": scratch_ok,
        "asr_stack_ok": stack_ok,
        "python": platform.python_version(),
    }


def cmd_run(arg: str) -> int:
    """Inside the Slurm job: transcribe every file, write result.json, drop the audio."""
    d = _job_dir(arg)
    try:
        job = read_job(d)
        import torch  # noqa: PLC0415 — only the GPU job pays for this import

        if not torch.cuda.is_available():
            # The model would quietly run on 2 CPU cores and take hours.
            print("[server] CUDA is not available inside the job — refusing to run on CPU.",
                  file=sys.stderr)
            return 1

        from .transcribe import Transcriber  # noqa: PLC0415

        started = time.monotonic()
        transcriber = Transcriber(model_id=str(MODEL_DIR), device="cuda")
        _ = transcriber.model  # load now: Transcriber falls back to CPU on a load-time OOM
        if transcriber.device != "cuda":
            print("[server] the model ended up on the CPU (CUDA out of memory at load) — "
                  "refusing to hold the GPU at CPU speed.", file=sys.stderr)
            return 1

        segments: dict[str, list[dict[str, Any]]] = {}
        for entry in job["audio"]:
            role = entry["role"]
            print(f"[server] {role}: {entry['seconds'] / 60:.1f} min", file=sys.stderr, flush=True)
            segs = transcriber.transcribe(
                d / entry["file"],
                vocab_context=job.get("vocab_context", ""),
                chunk_minutes=float(job.get("chunk_minutes", 3.0)),
                partial_path=d / f"partial_{role}.md",
            )
            segments[role] = [asdict(s) for s in segs]

        result = {
            "protocol": PROTOCOL,
            "version": __version__,
            "model": str(MODEL_DIR.resolve()),
            "device": transcriber.device,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "segments": segments,
        }
        tmp = d / "result.json.tmp"
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / "result.json")
        print(f"[server] done in {result['elapsed_seconds']} s", file=sys.stderr)
        return 0
    finally:
        _drop_audio(d)
        print("[server] audio deleted", file=sys.stderr)


# --------------------------------------------------------------------- entry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meetingmind-asr",
        description="Server side of meeting_mind's spark transcription. Prints one JSON line.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("new-job", help="create a job directory").add_argument("name")
    for name in ("submit", "status", "cancel", "clean", "run"):
        sub.add_parser(name).add_argument("job_dir")
    sub.add_parser("queue", help="GPU queue summary")
    sub.add_parser("doctor", help="check the server-side installation")
    sub.add_parser("lab", help="the lab cloud's address and meetings folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args.job_dir)
        if args.command == "new-job":
            out = cmd_new_job(args.name)
        elif args.command == "queue":
            out = cmd_queue()
        elif args.command == "doctor":
            out = cmd_doctor()
        elif args.command == "lab":
            out = cmd_lab()
        else:
            out = {"submit": cmd_submit, "status": cmd_status, "cancel": cmd_cancel,
                   "clean": cmd_clean}[args.command](args.job_dir)
    except JobError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc), **exc.extra}, ensure_ascii=False))
        return exc.exit_code
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
