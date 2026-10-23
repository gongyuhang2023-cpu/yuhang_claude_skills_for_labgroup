"""Test server — what `meetingmind-asr` does on spark, with Slurm and the model faked."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from meetingmind import server
from meetingmind.server import JobError
from meetingmind.transcribe import Segment

# --------------------------------------------------------------------- helpers


def _job(d: Path, roles: tuple[str, ...] = ("main",), **overrides: Any) -> dict[str, Any]:
    d.mkdir(parents=True, exist_ok=True)
    audio = []
    for role in roles:
        data = f"fake flac {role}".encode()
        (d / f"{role}.flac").write_bytes(data)
        audio.append({"role": role, "file": f"{role}.flac",
                      "sha256": hashlib.sha256(data).hexdigest(), "seconds": 600.0})
    job = {"protocol": server.PROTOCOL, "audio": audio, "vocab_context": "", "chunk_minutes": 3.0}
    job.update(overrides)
    (d / "job.json").write_text(json.dumps(job), encoding="utf-8")
    return job


class FakeSlurm:
    """Replaces server._run: answers squeue / sacct / sbatch / scancel from fields.

    Mirrors what the real commands do on spark (checked 2026-10-23): squeue -j
    on a job it no longer lists exits 1 with "Invalid job id specified"; sacct
    on an unknown id exits 0 with no output.
    """

    def __init__(self) -> None:
        self.queue: dict[str, str] = {}          # jid -> "STATE|reason|elapsed|limit"
        self.listing: list[str] = []             # squeue -o "%A|%T|%b|%Q|%M|%l" lines
        self.held: set[str] = set()              # jids with a memguard-hold placeholder
        self.sacct: dict[str, str] = {}
        self.sbatch_reply = (0, "1234\n", "")
        self.down = False                        # slurmctld not answering
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.argvs.append(argv)
        if self.down and argv[0] in ("squeue", "sacct", "scancel", "sbatch"):
            return 1, "", "slurm_load_jobs error: Unable to contact slurm controller"
        if argv[0] == "squeue" and "-j" in argv:
            line = self.queue.get(argv[argv.index("-j") + 1])
            if line is None:
                return 1, "", "slurm_load_jobs error: Invalid job id specified\n"
            return 0, line + "\n", ""
        if argv[0] == "squeue" and "-n" in argv:
            jid = argv[argv.index("-n") + 1].removeprefix("memguard-hold-")
            return 0, "999\n" if jid in self.held else "", ""
        if argv[0] == "squeue":
            return 0, "\n".join(self.listing), ""
        if argv[0] == "sacct":
            return 0, self.sacct.get(argv[argv.index("-j") + 1], ""), ""
        if argv[0] == "sbatch":
            return self.sbatch_reply
        if argv[0] == "scancel":
            return 0, "", ""
        raise AssertionError(argv)


@pytest.fixture
def slurm(monkeypatch: pytest.MonkeyPatch) -> FakeSlurm:
    fake = FakeSlurm()
    monkeypatch.setattr(server, "_run", fake)
    # Job directories are POSIX paths on the server; here any test dir will do.
    monkeypatch.setattr(server, "_job_dir", lambda arg: Path(arg))
    return fake


# --------------------------------------------------------------------- pure parts


@pytest.mark.parametrize(("name", "slug"), [
    ("2026-10-23-lab-meeting", "2026-10-23-lab-meeting"),
    ("组会 Weekly Sync!!", "weekly-sync"),
    ("组会", "meeting"),
    ("x" * 80, "x" * 40),
])
def test_slugify(name: str, slug: str) -> None:
    assert server.slugify(name) == slug


def test_read_job_accepts_a_well_formed_job(tmp_path: Path) -> None:
    _job(tmp_path, ("main", "mic"))
    assert [a["role"] for a in server.read_job(tmp_path)["audio"]] == ["main", "mic"]


@pytest.mark.parametrize("overrides", [
    {"protocol": 99},
    {"audio": []},
    {"audio": [{"role": "boss", "file": "boss.flac", "sha256": "0" * 64, "seconds": 1}]},
    {"audio": [{"role": "main", "file": "../../x.flac", "sha256": "0" * 64, "seconds": 1}]},
    {"audio": [{"role": "main", "file": "main.flac", "sha256": "abc", "seconds": 1}]},
    {"audio": [{"role": "main", "file": "main.flac", "sha256": "0" * 64, "seconds": 0}]},
    # beyond what the 20G budget was measured for
    {"audio": [{"role": "main", "file": "main.flac", "sha256": "0" * 64, "seconds": 9 * 3600}]},
    {"chunk_minutes": 0},
    {"chunk_minutes": 6},
    {"vocab_context": 5},
])
def test_read_job_refuses_anything_malformed(tmp_path: Path, overrides: dict[str, Any]) -> None:
    _job(tmp_path, **overrides)
    with pytest.raises(JobError) as excinfo:
        server.read_job(tmp_path)
    if "protocol" in overrides:
        assert excinfo.value.code == "protocol" and excinfo.value.exit_code == 3


def test_time_limit_scales_with_audio_and_has_a_floor() -> None:
    assert server.time_limit_minutes(60) == server.TIME_MIN
    assert server.time_limit_minutes(3600) == server.TIME_FIXED_MIN + 15
    assert server.time_limit_minutes(3 * 3600) == server.TIME_FIXED_MIN + 45


def test_sbatch_script_requests_the_gpu_and_declares_memory(tmp_path: Path) -> None:
    script = server.sbatch_script(Path("/scratch/meetingmind/u/j1"), 25)
    assert script.startswith("#!/bin/bash -l\n")
    for line in ("#SBATCH --gres=gpu:1", f"#SBATCH --mem={server.JOB_MEM}",
                 "#SBATCH -t 00:25:00", "#SBATCH -o /scratch/meetingmind/u/j1/slurm.log",
                 "D=/scratch/meetingmind/u/j1", "export HF_HUB_OFFLINE=1"):
        assert line in script.splitlines()
    assert script.rstrip().endswith('-m meetingmind.server run "$D"')


def test_sbatch_script_keeps_every_audio_copy_inside_the_job(tmp_path: Path) -> None:
    """Chunk WAVs go to the job's own tmp/, and a TERM (time limit, scancel)
    still runs the cleanup — /tmp would outlive the job, home would be backed up."""
    lines = server.sbatch_script(Path("/scratch/meetingmind/u/j1"), 25).splitlines()
    assert 'export TMPDIR="$D/tmp"' in lines
    assert "trap cleanup EXIT" in lines
    assert "trap 'exit 143' TERM INT" in lines
    cleanup = next(ln for ln in lines if ln.startswith("cleanup()"))
    assert '"$D/tmp"' in cleanup and '"$D/main.flac"' in cleanup and '"$D/mic.flac"' in cleanup
    assert not any(ln.startswith("exec ") for ln in lines), "exec would drop the traps"


@pytest.mark.parametrize(("arg", "ok"), [
    ("/scratch/meetingmind/alice/lab-meeting-20261023-150102-0a1b", True),
    ("/scratch/meetingmind/alice/..", False),                       # the shared root itself
    ("/scratch/meetingmind/alice/../bob/x-20261023-150102-0a1b", False),
    ("/scratch/meetingmind/alice/./lab-meeting-20261023-150102-0a1b", False),
    ("/scratch/meetingmind/bob/lab-meeting-20261023-150102-0a1b", False),  # someone else's
    ("/scratch/meetingmind/alice/notes", False),                    # not a job directory
    ("/scratch/meetingmind/alice/lab-meeting-20261023-150102-0a1b/x", False),
    ("relative/lab-meeting-20261023-150102-0a1b", False),
])
def test_only_own_job_directories_are_accepted(arg: str, ok: bool) -> None:
    assert server.is_job_path(arg, "/scratch/meetingmind/alice") is ok


def test_progress_is_the_last_chunk_reported(tmp_path: Path) -> None:
    log = tmp_path / "slurm.log"
    log.write_text("[transcribe] chunk 1/4 [..]\nnoise\n[transcribe] chunk 3/4 [..]\n", encoding="utf-8")
    assert server.progress_of(log) == "3/4"
    assert server.progress_of(tmp_path / "missing.log") is None


# --------------------------------------------------------------------- submit / status / cancel


def test_submit_checks_every_checksum_before_queueing(tmp_path: Path, slurm: FakeSlurm) -> None:
    _job(tmp_path)
    (tmp_path / "main.flac").write_bytes(b"truncated")
    with pytest.raises(JobError) as excinfo:
        server.cmd_submit(str(tmp_path))
    assert (excinfo.value.code, excinfo.value.extra["file"]) == ("checksum", "main.flac")
    assert not any(a[0] == "sbatch" for a in slurm.argvs)


def test_submit_queues_once(tmp_path: Path, slurm: FakeSlurm) -> None:
    _job(tmp_path)
    out = server.cmd_submit(str(tmp_path))
    assert out["job_id"] == "1234"
    assert (tmp_path / "slurm_job_id").read_text(encoding="utf-8") == "1234"
    assert "#SBATCH --gres=gpu:1" in (tmp_path / "job.sbatch").read_text(encoding="utf-8")

    slurm.queue["1234"] = "PENDING|Priority|0:00|25:00"
    again = server.cmd_submit(str(tmp_path))
    assert again == {"job_id": "1234", "already_submitted": True}
    assert sum(1 for a in slurm.argvs if a[0] == "sbatch") == 1


def test_submit_reports_an_sbatch_refusal(tmp_path: Path, slurm: FakeSlurm) -> None:
    _job(tmp_path)
    slurm.sbatch_reply = (1, "", "sbatch: error: Batch job submission failed")
    with pytest.raises(JobError, match="submission failed"):
        server.cmd_submit(str(tmp_path))


def test_status_pending_counts_gpu_jobs_ahead(tmp_path: Path, slurm: FakeSlurm) -> None:
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    slurm.queue["50"] = "PENDING|Resources|0:00|25:00"
    slurm.listing = [
        "40|RUNNING|gres/gpu:1|900|2:00:00|1-00:00:00",   # holds the GPU
        "45|PENDING|gres/gpu:1|800|0:00|1:00:00",          # same priority, earlier: ahead
        "60|PENDING|gres/gpu:1|900|0:00|1:00:00",          # higher priority: ahead despite id
        "47|PENDING|gres/gpu:1|500|0:00|1:00:00",          # lower priority: behind
        "41|RUNNING|N/A|999|10:00|2:00:00",                # CPU job: irrelevant
        "50|PENDING|gres/gpu:1|800|0:00|25:00",            # us
        "garbage line",
    ]
    st = server.cmd_status(str(tmp_path))
    assert (st["state"], st["reason"], st["ahead"]) == ("PENDING", "Resources", 3)


def test_status_without_slurm_decides_nothing(tmp_path: Path, slurm: FakeSlurm) -> None:
    """A slurmctld hiccup must not read as "the job is gone" — that reading deletes."""
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    slurm.down = True
    with pytest.raises(JobError) as excinfo:
        server.cmd_status(str(tmp_path))
    assert excinfo.value.code == "slurm_unavailable"


def test_pending_without_a_queue_listing_still_reports_state(tmp_path: Path, slurm: FakeSlurm,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    slurm.queue["50"] = "PENDING|Priority|0:00|25:00"
    real = slurm.__call__

    def listing_fails(argv: list[str]) -> tuple[int, str, str]:
        if argv[0] == "squeue" and "-j" not in argv and "-n" not in argv:
            return 1, "", "timeout"
        return real(argv)

    monkeypatch.setattr(server, "_run", listing_fails)
    st = server.cmd_status(str(tmp_path))
    assert st["state"] == "PENDING" and "ahead" not in st


def test_status_running_reports_chunk_progress(tmp_path: Path, slurm: FakeSlurm) -> None:
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    (tmp_path / "slurm.log").write_text("[transcribe] chunk 2/11 [x]\n", encoding="utf-8")
    slurm.queue["50"] = "RUNNING|None|0:45|25:00"
    st = server.cmd_status(str(tmp_path))
    assert (st["state"], st["progress"], st["elapsed"]) == ("RUNNING", "2/11", "0:45")
    assert "log_tail" not in st


def test_status_sees_a_frozen_job(tmp_path: Path, slurm: FakeSlurm) -> None:
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    slurm.queue["50"] = "RUNNING|None|9:00|25:00"
    slurm.held.add("50")
    assert server.cmd_status(str(tmp_path))["state"] == "FROZEN"


def test_status_after_the_job_left_the_queue(tmp_path: Path, slurm: FakeSlurm) -> None:
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    (tmp_path / "slurm.log").write_text("Traceback ...\nRuntimeError: boom\n", encoding="utf-8")
    slurm.sacct["50"] = "CANCELLED by 1001\n"
    st = server.cmd_status(str(tmp_path))
    assert st["state"] == "CANCELLED"
    assert "boom" in st["log_tail"]


def test_status_trusts_result_json_while_sacct_lags(tmp_path: Path, slurm: FakeSlurm) -> None:
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    st = server.cmd_status(str(tmp_path))
    assert (st["state"], st["result_ready"]) == ("COMPLETED", True)


def test_cancel_refuses_a_frozen_job(tmp_path: Path, slurm: FakeSlurm) -> None:
    """scancel on a frozen job leaves it stuck in COMPLETING, memory held."""
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    slurm.held.add("50")
    with pytest.raises(JobError) as excinfo:
        server.cmd_cancel(str(tmp_path))
    assert excinfo.value.code == "frozen" and "cancel-frozen" in str(excinfo.value)
    assert not any(a[0] == "scancel" for a in slurm.argvs)


def test_cancel_refuses_when_frozen_state_cannot_be_checked(tmp_path: Path, slurm: FakeSlurm) -> None:
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    slurm.down = True
    with pytest.raises(JobError) as excinfo:
        server.cmd_cancel(str(tmp_path))
    assert excinfo.value.code == "slurm_unavailable"
    assert not any(a[0] == "scancel" for a in slurm.argvs)


def test_clean_refuses_a_job_still_in_the_queue(tmp_path: Path, slurm: FakeSlurm) -> None:
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    slurm.queue["50"] = "RUNNING|None|1:00|25:00"
    with pytest.raises(JobError) as excinfo:
        server.cmd_clean(str(tmp_path))
    assert excinfo.value.code == "active"
    assert tmp_path.is_dir()

    slurm.queue["50"] = "CANCELLED|None|1:00|25:00"   # finished, still listed for MinJobAge
    assert server.cmd_clean(str(tmp_path)) == {"cleaned": True}
    assert not tmp_path.exists()


def test_clean_without_slurm_keeps_the_directory(tmp_path: Path, slurm: FakeSlurm) -> None:
    (tmp_path / "slurm_job_id").write_text("50", encoding="utf-8")
    slurm.down = True
    with pytest.raises(JobError):
        server.cmd_clean(str(tmp_path))
    assert tmp_path.is_dir()


def test_prune_removes_old_finished_jobs_only(tmp_path: Path, slurm: FakeSlurm) -> None:
    old = time.time() - (server.STALE_DAYS + 1) * 86400
    finished, queued, fresh = (tmp_path / n for n in ("finished", "queued", "fresh"))
    for d, jid in ((finished, "1"), (queued, "2"), (fresh, "3")):
        d.mkdir()
        (d / "slurm_job_id").write_text(jid, encoding="utf-8")
    for d in (finished, queued):
        os.utime(d, (old, old))
    slurm.queue["2"] = "PENDING|Resources|0:00|25:00"   # waited a week behind a long GPU job
    assert server._prune(tmp_path) == ["finished"]
    assert queued.is_dir() and fresh.is_dir()


def test_prune_without_slurm_removes_nothing(tmp_path: Path, slurm: FakeSlurm) -> None:
    old = time.time() - (server.STALE_DAYS + 1) * 86400
    d = tmp_path / "job"
    d.mkdir()
    (d / "slurm_job_id").write_text("1", encoding="utf-8")
    os.utime(d, (old, old))
    slurm.down = True
    assert server._prune(tmp_path) == []
    assert d.is_dir()


def test_queue_summary(slurm: FakeSlurm) -> None:
    slurm.listing = ["40|RUNNING|gres/gpu:1|900|2:00:00|1-00:00:00",
                     "45|PENDING|gres/gpu:1|800|0:00|1:00:00",
                     "41|RUNNING|N/A|999|10:00|2:00:00"]
    assert server.cmd_queue() == {
        "gpu_running": [{"elapsed": "2:00:00", "limit": "1-00:00:00"}], "gpu_pending": 1,
    }


def test_new_job_refuses_when_not_deployed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SCRATCH_ROOT", tmp_path / "absent")
    with pytest.raises(JobError) as excinfo:
        server.cmd_new_job("x")
    assert excinfo.value.code == "not_deployed"


def test_new_job_makes_a_private_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SCRATCH_ROOT", tmp_path)
    monkeypatch.setattr(server.getpass, "getuser", lambda: "alice")
    d = Path(server.cmd_new_job("Lab Meeting")["job_dir"])
    assert d.parent == tmp_path / "alice"
    assert d.name.startswith("lab-meeting-")
    if sys.platform != "win32":
        assert d.stat().st_mode & 0o777 == 0o700
        assert d.parent.stat().st_mode & 0o777 == 0o700


# --------------------------------------------------------------------- run (inside the job)


def test_run_writes_result_and_deletes_the_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 slurm: FakeSlurm) -> None:
    _job(tmp_path, ("main", "mic"), vocab_context="术语", chunk_minutes=2.0)
    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    calls: list[tuple[str, str, float]] = []

    class FakeTranscriber:
        device = "cuda"
        model = object()

        def __init__(self, model_id: str, device: str) -> None:
            assert device == "cuda"

        def transcribe(self, path: Path, vocab_context: str, chunk_minutes: float,
                       partial_path: Path) -> list[Segment]:
            calls.append((path.name, vocab_context, chunk_minutes))
            return [Segment(0.0, 120.0, f"text of {path.name}")]

    monkeypatch.setattr("meetingmind.transcribe.Transcriber", FakeTranscriber)
    assert server.cmd_run(str(tmp_path)) == 0

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["protocol"] == server.PROTOCOL
    assert result["segments"]["mic"] == [
        {"start": 0.0, "end": 120.0, "text": "text of mic.flac", "speaker": None},
    ]
    assert calls == [("main.flac", "术语", 2.0), ("mic.flac", "术语", 2.0)]
    assert not (tmp_path / "main.flac").exists() and not (tmp_path / "mic.flac").exists()


def test_run_refuses_to_crawl_along_on_cpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           slurm: FakeSlurm) -> None:
    _job(tmp_path)
    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert server.cmd_run(str(tmp_path)) == 1
    assert not (tmp_path / "main.flac").exists(), "the audio never outlives the job"


def test_run_refuses_a_model_that_fell_back_to_cpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   slurm: FakeSlurm) -> None:
    """A load-time CUDA OOM makes Transcriber retry on CPU — holding the GPU at CPU speed."""
    _job(tmp_path)
    monkeypatch.setitem(sys.modules, "torch",
                        types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True)))

    class FellBack:
        def __init__(self, model_id: str, device: str) -> None:
            self.device = device

        @property
        def model(self) -> object:
            self.device = "cpu"
            return object()

        def transcribe(self, *args: Any, **kwargs: Any) -> list[Segment]:
            raise AssertionError("must not transcribe on CPU")

    monkeypatch.setattr("meetingmind.transcribe.Transcriber", FellBack)
    assert server.cmd_run(str(tmp_path)) == 1
    assert not (tmp_path / "result.json").exists()
    assert not (tmp_path / "main.flac").exists()


def test_run_crash_still_deletes_the_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           slurm: FakeSlurm) -> None:
    _job(tmp_path)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "chunk_001.wav").write_bytes(b"RIFF")
    monkeypatch.setitem(sys.modules, "torch",
                        types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True)))

    class Boom:
        device = "cuda"
        model = object()

        def __init__(self, model_id: str, device: str) -> None:
            pass

        def transcribe(self, *args: Any, **kwargs: Any) -> list[Segment]:
            raise RuntimeError("ASR failed on chunk 3/11")

    monkeypatch.setattr("meetingmind.transcribe.Transcriber", Boom)
    with pytest.raises(RuntimeError, match="chunk 3/11"):
        server.cmd_run(str(tmp_path))
    assert not (tmp_path / "main.flac").exists()
    assert not (tmp_path / "tmp").exists()


def test_lab_settings_come_from_the_server_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = tmp_path / "lab.json"
    monkeypatch.setattr(server, "LAB_FILE", lab)
    with pytest.raises(JobError) as excinfo:
        server.cmd_lab()
    assert excinfo.value.code == "not_configured"

    lab.write_text(json.dumps({"cloud_url": "https://cloud.example.org/", "cloud_folder": "/lab/m/"}),
                   encoding="utf-8")
    assert server.cmd_lab() == {"cloud_url": "https://cloud.example.org", "cloud_folder": "/lab/m"}

    lab.write_text(json.dumps({"cloud_url": "http://plain.example.org", "cloud_folder": "x"}),
                   encoding="utf-8")
    with pytest.raises(JobError, match="lacks"):
        server.cmd_lab()


def test_main_prints_refusals_as_json(capsys: pytest.CaptureFixture[str],
                                      monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "SCRATCH_ROOT", tmp_path / "absent")
    assert server.main(["new-job", "x"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "not_deployed"
