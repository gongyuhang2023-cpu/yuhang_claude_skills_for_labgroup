"""Test H — kill the process outright; the recording must survive.

This is the closest automated reproduction of what happened on 2026-08-03: a
recording process that stops existing without ever running its teardown. Under
the old design that lost everything, because the WAV was only created inside
stop(). The bar now is that whatever was captured is on disk and readable.

`Popen.kill()` is `TerminateProcess` on Windows — no cleanup handlers, no
atexit, the same abruptness as the original failure.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from meetingmind.recovery import inspect_wav, repair_wav

CHILD = Path(__file__).with_name("hardkill_child.py")
PRELUDE_FRAMES = 48_000 * 5


def _run_until_ready(out: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(CHILD), str(out)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    if line != "READY":  # pragma: no cover - child failed to start
        proc.kill()
        raise AssertionError(f"child never became ready (said {line!r})")
    return proc


@pytest.mark.slow
@pytest.mark.parametrize("run", range(20))
def test_killed_recording_is_still_readable(tmp_path: Path, run: int) -> None:
    """Twenty runs, killed at slightly different moments.

    The repetitions are the point: the window where the two header length
    fields disagree is microseconds wide, so a single run would almost always
    miss it and prove less than it appears to.
    """
    out = tmp_path / f"kill-{run}.wav"
    proc = _run_until_ready(out)
    # Vary the instant of death across runs to sample different points in the
    # write/patch cycle. The child is expected to still be running.
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=0.02 + run * 0.007)
    proc.kill()
    proc.wait(timeout=10)

    assert out.is_file(), "the WAV must exist even though stop() never ran"

    status = inspect_wav(out)
    assert status.status in ("ok", "repairable"), f"{status.status}: {status.detail}"
    if status.status == "repairable":
        status = repair_wav(out, force=True)
        assert status.status == "ok"

    with wave.open(str(out), "rb") as wf:
        frames = wf.getnframes()
        assert frames >= PRELUDE_FRAMES, (
            f"only {frames} frames survived; the child had written at least "
            f"{PRELUDE_FRAMES} before announcing itself"
        )
        # Decoding the whole payload proves the length fields agree with reality.
        assert len(wf.readframes(frames)) == frames * 4
