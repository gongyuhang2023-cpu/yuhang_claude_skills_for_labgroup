"""Test D — memory must stay bounded regardless of how long the meeting runs.

The incident's second failure was that peak memory scaled with total recording
length (~12 GB for 115 minutes). Real time is irrelevant to that property, so
these tests push a full meeting's worth of audio through as fast as the machine
allows and watch RSS the whole way — which is strictly harsher than a real
recording, since there are no idle gaps for the allocator to catch up in.

They do NOT prove long-run reliability (page-cache behaviour, antivirus stalls,
device drift). That is a post-merge endurance check on a real meeting.
"""

from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np
import pytest

from meetingmind.wavsink import WavSink, _float32_to_int16
from tests.fakes import null_opener, slow_opener
from tests.helpers import RSSSampler, make_float32_samples, to_pcm_bytes

SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 4  # float32 input

# Ceiling for RSS growth. The queue alone is allowed 60 s (~23 MB) and a batch
# plus its float64 RMS temporary adds a few tens of MB; 200 MB leaves room for
# allocator noise while still being ~60x below the old peak.
MAX_RSS_GROWTH_MB = 200.0


def _block(seconds: float = 0.1) -> bytes:
    n = int(SAMPLE_RATE * seconds) * CHANNELS
    return to_pcm_bytes(make_float32_samples(n))


@pytest.mark.slow
def test_two_hours_at_sustainable_rate_loses_nothing(wav_path: Path) -> None:
    """Two hours of audio at the fastest rate the writer can actually sustain.

    A real device paces itself at 1x; this pushes the same total volume as fast
    as the writer will take it (hundreds of times faster than real time) while
    respecting backpressure, which is what a healthy long recording looks like.
    Nothing may be lost and memory must not track the total.
    """
    total_seconds = 2 * 60 * 60
    block = _block(0.1)
    n_blocks = int(total_seconds / 0.1)

    sink = WavSink(
        wav_path, SAMPLE_RATE, CHANNELS, opener=null_opener(), name="bounds"
    )
    sink.start()
    watermark = sink.stats()["max_queued_bytes"] // 2

    with RSSSampler(interval=0.02) as rss:
        for _ in range(n_blocks):
            while sink.stats()["queued_bytes"] > watermark:
                time.sleep(0.001)
            sink.feed(block)
        sink.finish(timeout=120)

    assert sink.writer_error is None
    assert sink.dropped_bytes == 0, "a writer that keeps up must never shed data"
    assert sink.peak_queued_bytes <= sink.stats()["max_queued_bytes"]
    assert sink.written_frames == n_blocks * int(SAMPLE_RATE * 0.1)
    assert rss.growth_mb < MAX_RSS_GROWTH_MB, (
        f"RSS grew {rss.growth_mb:.1f} MB feeding {total_seconds / 3600:.1f}h "
        f"of audio; the queue cap is "
        f"{sink.stats()['max_queued_bytes'] / 1e6:.1f} MB"
    )


@pytest.mark.slow
def test_extreme_overrun_sheds_instead_of_growing(wav_path: Path) -> None:
    """Two hours shoved in with no backpressure at all — memory must still hold.

    At roughly a thousand times real time no writer can keep up, so the sink is
    expected to drop. What matters is that it drops rather than buffering: this
    is the exact failure mode that cost 115 minutes of audio, reproduced at the
    worst plausible severity.
    """
    block = _block(0.1)
    n_blocks = int((2 * 60 * 60) / 0.1)

    sink = WavSink(
        wav_path, SAMPLE_RATE, CHANNELS, opener=null_opener(), name="flood"
    )
    sink.start()
    with RSSSampler(interval=0.02) as rss:
        for _ in range(n_blocks):
            sink.feed(block)
        sink.finish(timeout=120)

    assert sink.dropped_bytes > 0
    assert sink.peak_queued_bytes <= sink.stats()["max_queued_bytes"]
    assert rss.growth_mb < MAX_RSS_GROWTH_MB, (
        f"RSS grew {rss.growth_mb:.1f} MB under flood; queue cap is "
        f"{sink.stats()['max_queued_bytes'] / 1e6:.1f} MB"
    )
    # Whatever survived must be accounted for exactly.
    assert (
        sink.stats()["accepted_bytes"] + sink.dropped_bytes
        == n_blocks * len(block)
    )


@pytest.mark.slow
def test_queue_cap_holds_when_producer_outruns_writer(wav_path: Path) -> None:
    """A stalled disk must cost audio, not unbounded memory.

    Dropping is the deliberate trade: blocking the caller would stall the
    capture thread and lose audio at the device instead, which is worse and
    invisible.
    """
    sink = WavSink(
        wav_path,
        SAMPLE_RATE,
        CHANNELS,
        max_buffer_seconds=0.5,
        batch_bytes=16 << 10,
        opener=slow_opener(delay=0.02),
        name="slowdisk",
    )
    sink.start()

    block = _block(0.05)
    with RSSSampler(interval=0.02) as rss:
        for _ in range(400):  # 20 s of audio at a disk doing far less
            sink.feed(block)
        sink.finish(timeout=60)

    assert sink.dropped_bytes > 0, "the slow disk should have forced drops"
    assert sink.first_drop_at_frame is not None
    assert sink.peak_queued_bytes <= sink.stats()["max_queued_bytes"]
    assert rss.growth_mb < MAX_RSS_GROWTH_MB

    # Whatever did get written must still be a valid file, and the part before
    # the first drop must be exactly right.
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == sink.written_frames
        prefix = wf.readframes(sink.first_drop_at_frame)
    expected = _float32_to_int16(
        np.frombuffer(block, dtype="<f4")
    ).tobytes()
    n = min(len(prefix), len(expected))
    assert prefix[:n] == expected[:n]


def test_feed_never_blocks_even_when_the_queue_is_full(wav_path: Path) -> None:
    """The bounded wait in FeedGate (and the capture threads) depends on this."""
    import time

    sink = WavSink(
        wav_path,
        SAMPLE_RATE,
        CHANNELS,
        max_buffer_seconds=0.05,
        opener=slow_opener(delay=0.05),
        name="nonblocking",
    )
    sink.start()
    block = _block(0.1)  # each block alone exceeds the cap

    slowest = 0.0
    for _ in range(200):
        t0 = time.perf_counter()
        sink.feed(block)
        slowest = max(slowest, time.perf_counter() - t0)

    sink.finish(timeout=60)
    assert sink.dropped_bytes > 0
    assert slowest < 0.05, f"feed() blocked for {slowest*1000:.1f} ms"
