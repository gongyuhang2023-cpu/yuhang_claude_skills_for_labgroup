"""Deterministic audio generators and measurement helpers.

Everything here is synthetic: no sound card, no window, no meeting software.
The incident this refactor addresses (2026-08-03, ~115 minutes of Teams audio
lost because the buffer only reached disk at stop()) was a pure software
failure, so it must be reproducible in a pure software test.

Kept out of conftest.py on purpose: pytest imports conftest under its own
module name, so a test doing `from tests.conftest import ...` would load a
second copy of it. Helpers live here; conftest.py holds only fixtures.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import psutil

# A stereo float32 frame is 8 bytes; several tests deliberately hand the sink
# chunk sizes that are NOT multiples of it, to exercise residue carry-over.
UNALIGNED_CHUNK_SIZES: tuple[int, ...] = (7, 13, 4801, 3, 19207)


def make_float32_samples(n: int, *, seed: int = 1234) -> np.ndarray:
    """Deterministic pseudo-random float32 PCM in [-1, 1] with edge values.

    The edge values matter: ±1.0 pins the int16 conversion's symmetric scaling,
    exact 0.0 pins silence handling, and the ~1e-9 values pin the "RMS must be
    accumulated from the original float32, not the quantised int16" rule — they
    quantise to int16 zero but are not zero in float64.
    """
    rng = np.random.default_rng(seed)
    x = rng.uniform(-1.0, 1.0, size=n).astype(np.float32)
    edges = np.array(
        [1.0, -1.0, 0.0, 1e-9, -1e-9, 0.5, -0.5, 3.0517578125e-05],
        dtype=np.float32,
    )
    k = min(len(edges), n)
    x[:k] = edges[:k]
    return x


def to_pcm_bytes(samples: np.ndarray) -> bytes:
    """Interleaved little-endian float32 bytes — the WavSink.feed() wire format."""
    return np.ascontiguousarray(samples, dtype="<f4").tobytes()


def chunked(
    data: bytes, sizes: tuple[int, ...] = UNALIGNED_CHUNK_SIZES
) -> Iterator[bytes]:
    """Slice `data` into chunks cycling through `sizes`.

    Default sizes are all non-multiples of 8, so frame boundaries land inside
    chunks and the sink's residue path is exercised on essentially every chunk.
    """
    pos = 0
    i = 0
    while pos < len(data):
        n = sizes[i % len(sizes)]
        yield data[pos : pos + n]
        pos += n
        i += 1


def legacy_int16_bytes(samples: np.ndarray, channels: int) -> bytes:
    """Reference output using the pre-refactor algorithm.

    `audio.py` used to join every captured chunk, truncate the tail to whole
    frames once at the end, then convert in one shot. New code converts
    incrementally while carrying a residue across batches; the two must agree
    byte-for-byte. This is the golden reference for that comparison.
    """
    usable = (samples.size // channels) * channels
    trimmed = samples[:usable]
    int16 = np.clip(trimmed * 32767.0, -32768.0, 32767.0).astype(np.int16)
    return int16.tobytes()


class RSSSampler:
    """Background sampler for this process's resident set size.

    Peak RSS is the only direct evidence that the sink is memory-bounded.
    Sampling only at the end would miss the spike we are guarding against —
    the old code's peak lived for a few seconds inside _write_wav() and then
    collapsed as its temporaries were freed.
    """

    def __init__(self, interval: float = 0.05) -> None:
        self._interval = interval
        self._proc = psutil.Process()
        self._samples: list[int] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.baseline: int = 0

    def __enter__(self) -> RSSSampler:
        self.baseline = self._proc.memory_info().rss
        self._samples = [self.baseline]
        self._thread = threading.Thread(
            target=self._run, name="rss-sampler", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._samples.append(self._proc.memory_info().rss)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._samples.append(self._proc.memory_info().rss)
            except psutil.Error:  # pragma: no cover - process is going away
                return

    @property
    def peak(self) -> int:
        return max(self._samples) if self._samples else 0

    @property
    def growth(self) -> int:
        """Peak minus the baseline taken on entry — the number tests assert on."""
        return self.peak - self.baseline

    @property
    def growth_mb(self) -> float:
        return self.growth / (1024 * 1024)


def wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01
) -> bool:
    """Poll `predicate` until true or `timeout` elapses. Returns whether it held.

    Used instead of bare sleeps so concurrency tests stay fast when things go
    right and still fail (rather than hang) when they go wrong.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
