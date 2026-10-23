"""Child process for the hard-kill test: record until somebody kills us.

Writes five seconds of synthetic audio, announces READY, then keeps feeding so
the parent can terminate it at an arbitrary point — including, with enough
repetitions, between the two header length fields.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

from meetingmind.wavsink import WavSink

SR, CH = 48_000, 2
PRELUDE_SECONDS = 5


def main() -> None:
    out = Path(sys.argv[1])
    sink = WavSink(out, SR, CH, batch_bytes=64 << 10, name="hardkill")
    sink.start()

    rng = np.random.default_rng(3)
    block = np.ascontiguousarray(
        rng.uniform(-1.0, 1.0, int(SR * 0.1) * CH), dtype="<f4"
    ).tobytes()

    for _ in range(PRELUDE_SECONDS * 10):
        sink.feed(block)

    deadline = time.monotonic() + 30
    while sink.written_frames < SR * PRELUDE_SECONDS:
        if time.monotonic() > deadline:  # pragma: no cover - writer wedged
            print("TIMEOUT", flush=True)
            return
        time.sleep(0.005)

    print("READY", flush=True)
    while True:
        sink.feed(block)
        time.sleep(0.002)


if __name__ == "__main__":
    main()
