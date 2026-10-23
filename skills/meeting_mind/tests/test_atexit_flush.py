"""The atexit safety net: exiting without stop() must not drop queued audio.

The writer thread is a daemon so a bug in it cannot hang the process. The price
is that Python kills it wherever it stands at shutdown — so a `record` run that
dies on an unhandled exception would lose whatever had not been written yet.
An atexit handler runs before daemon threads are killed, which is the only
place to close that gap.
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import pytest

CHILD = Path(__file__).with_name("atexit_child.py")
EXPECTED_FRAMES = 48_000 * 4  # the child feeds four seconds


@pytest.mark.parametrize("mode", ["clean", "crash"])
def test_queued_audio_is_flushed_on_exit(tmp_path: Path, mode: str) -> None:
    out = tmp_path / f"{mode}.wav"
    proc = subprocess.run(
        [sys.executable, str(CHILD), str(out), mode],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if mode == "crash":
        assert proc.returncode != 0
        assert "simulated unhandled exception" in proc.stderr
    else:
        assert proc.returncode == 0, proc.stderr

    assert out.is_file()
    with wave.open(str(out), "rb") as wf:
        frames = wf.getnframes()
    assert frames == EXPECTED_FRAMES, (
        f"{frames} of {EXPECTED_FRAMES} frames survived — the atexit flush did "
        f"not drain the queue before the daemon writer was killed"
    )
