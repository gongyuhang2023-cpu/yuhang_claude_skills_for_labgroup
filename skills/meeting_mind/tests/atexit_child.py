"""Child process for the atexit test: feed a burst, then leave without stopping.

Two modes, both of which used to lose whatever was still queued:
  clean  — return from main() normally, never calling finish()
  crash  — die on an unhandled exception, the way `record` would on a bug
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from meetingmind.wavsink import WavSink

SR, CH = 48_000, 2
BLOCKS = 40  # 4 seconds at 0.1s per block


def main() -> None:
    out = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "clean"

    sink = WavSink(out, SR, CH, batch_bytes=16 << 10, name="atexit")
    sink.start()

    block = np.ascontiguousarray(
        np.random.default_rng(11).uniform(-1.0, 1.0, int(SR * 0.1) * CH), dtype="<f4"
    ).tobytes()
    for _ in range(BLOCKS):
        sink.feed(block)

    # Deliberately no finish(): exit while the writer still has work queued.
    if mode == "crash":
        raise RuntimeError("simulated unhandled exception")


if __name__ == "__main__":
    main()
