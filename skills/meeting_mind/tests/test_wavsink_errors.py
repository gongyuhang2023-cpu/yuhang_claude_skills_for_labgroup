"""Test E — a failed write must abort loudly and leave a valid, truncated file.

The policy is "no retry": a write can fail after putting some bytes in the
file, so replaying the batch would duplicate them, and the realistic cause
(disk full) will not succeed on a second attempt. Aborting is affordable only
because everything already written is safe on disk — which is exactly what this
refactor bought.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest

from meetingmind.wavsink import WavSink
from tests.fakes import (
    blocking_write_opener,
    faulty_flush_opener,
    faulty_opener,
    short_write_opener,
)
from tests.helpers import chunked, make_float32_samples, to_pcm_bytes

# start() writes the header in three calls (RIFF, fmt struct, data length);
# each batch then costs one data write plus two header patches.
_HEADER_WRITES = 3


def _feed_a_lot(sink: WavSink, n_samples: int = 200_000) -> None:
    for chunk in chunked(to_pcm_bytes(make_float32_samples(n_samples))):
        sink.feed(chunk)


def test_write_failure_aborts_and_leaves_a_valid_file(wav_path: Path) -> None:
    sink = WavSink(
        wav_path,
        48000,
        2,
        batch_bytes=8 << 10,
        opener=faulty_opener(fail_on_write=_HEADER_WRITES + 4),
    )
    sink.start()
    _feed_a_lot(sink)
    sink.finish()

    assert sink.writer_error is not None
    assert "No space left" in sink.writer_error
    assert sink.written_frames > 0, "the batches before the failure must survive"

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == sink.written_frames


def test_declared_length_never_exceeds_reality_after_failure(wav_path: Path) -> None:
    """On failure we skip the header patch, so the file under-reports, never over."""
    sink = WavSink(
        wav_path,
        48000,
        2,
        batch_bytes=8 << 10,
        opener=faulty_opener(fail_on_write=_HEADER_WRITES + 7),
    )
    sink.start()
    _feed_a_lot(sink)
    sink.finish()

    raw = wav_path.read_bytes()
    data_declared = int.from_bytes(raw[40:44], "little")
    assert data_declared <= len(raw) - 44


def test_flush_failure_is_reported(wav_path: Path) -> None:
    sink = WavSink(
        wav_path,
        48000,
        2,
        batch_bytes=8 << 10,
        opener=faulty_flush_opener(fail_on_flush=3),
    )
    sink.start()
    _feed_a_lot(sink)
    sink.finish()

    assert sink.writer_error is not None


def test_feed_after_finish_is_counted_and_changes_nothing(wav_path: Path) -> None:
    """The load-bearing invariant: late callbacks are harmless, not fatal."""
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.feed(to_pcm_bytes(make_float32_samples(4_800)))
    sink.finish()

    before_hash = hashlib.sha256(wav_path.read_bytes()).hexdigest()
    before_mtime = wav_path.stat().st_mtime_ns

    late = to_pcm_bytes(make_float32_samples(1_000))
    for _ in range(100):
        sink.feed(late)  # must not raise

    assert sink.fed_after_close_bytes == 100 * len(late)
    assert hashlib.sha256(wav_path.read_bytes()).hexdigest() == before_hash
    assert wav_path.stat().st_mtime_ns == before_mtime


def test_feed_before_start_does_not_crash(wav_path: Path) -> None:
    sink = WavSink(wav_path, 48000, 2)
    sink.feed(to_pcm_bytes(make_float32_samples(100)))  # must not raise
    assert not wav_path.exists()


def test_feed_after_writer_death_stops_accumulating(wav_path: Path) -> None:
    """A dead writer must not turn the queue back into an unbounded buffer.

    The writer closes the sink on its way out precisely so this cannot happen;
    the assertions below pin that, rather than the tautology of "feeding after
    close increments the after-close counter".
    """
    sink = WavSink(
        wav_path,
        48000,
        2,
        batch_bytes=4 << 10,
        opener=faulty_opener(fail_on_write=_HEADER_WRITES + 1),
    )
    sink.start()
    _feed_a_lot(sink, 50_000)
    sink.finish()

    assert sink.writer_error is not None
    queued_after_death = sink.stats()["queued_bytes"]

    late = to_pcm_bytes(make_float32_samples(1_000))
    for _ in range(50):
        sink.feed(late)

    # Nothing new may be retained: the queue must not have grown by a byte.
    assert sink.stats()["queued_bytes"] == queued_after_death
    assert sink.fed_after_close_bytes == 50 * len(late)
    # And whatever the dead writer left behind is bounded, not the whole feed.
    assert queued_after_death <= sink.stats()["max_queued_bytes"]


def test_size_limit_stops_at_the_boundary(wav_path: Path) -> None:
    limit = 1 << 20  # 1 MiB of int16 payload
    sink = WavSink(wav_path, 48000, 2, max_data_bytes=limit, batch_bytes=64 << 10)
    sink.start()
    _feed_a_lot(sink, 2_000_000)  # ~8 MB of float32 in, well past the cap
    sink.finish()

    assert sink.size_limit_reached is True
    assert sink.written_bytes == limit
    assert wav_path.stat().st_size == 44 + limit
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == limit // 4


def test_short_write_never_over_reports(wav_path: Path) -> None:
    """A partial write that does not raise must not be counted as a full one.

    Found by review, not by me: `write()` may store fewer bytes than asked and
    simply say so in its return value. Counting the requested length anyway
    would patch the header to claim bytes that are not in the file — the exact
    over-reporting the length fields are supposed to make impossible.
    """
    sink = WavSink(
        wav_path,
        48000,
        2,
        batch_bytes=8 << 10,
        opener=short_write_opener(short_on_write=_HEADER_WRITES + 1),
    )
    sink.start()
    _feed_a_lot(sink)
    sink.finish()

    assert sink.writer_error is not None
    assert "short write" in sink.writer_error

    raw = wav_path.read_bytes()
    data_declared = int.from_bytes(raw[40:44], "little")
    assert data_declared <= len(raw) - 44, "header claims bytes the file lacks"
    assert sink.written_bytes <= len(raw) - 44

    with wave.open(str(wav_path), "rb") as wf:
        assert len(wf.readframes(wf.getnframes())) == wf.getnframes() * 4


def test_short_write_in_the_header_fails_start(wav_path: Path) -> None:
    """The header's own writes must be checked too.

    Fixing only the payload write left the three header writes and the two
    length patches unchecked — an opener that halved every write produced a
    22-byte file with no error reported anywhere.
    """
    sink = WavSink(wav_path, 48000, 2, opener=short_write_opener(short_on_write=1))
    with pytest.raises(OSError, match="short write"):
        sink.start()
    assert not wav_path.exists(), "a truncated header must not be left behind"


def test_finish_timeout_stays_retryable(wav_path: Path) -> None:
    """A writer stuck in write() must not leave the sink permanently half-done.

    Reporting the timeout is right; declaring the sink finished is not. The
    file is still the writer's, the RMS has not been computed, and both a
    later finish() and the atexit flush have to be able to complete it.
    """
    opener, created = blocking_write_opener(block_on_write=_HEADER_WRITES + 1)
    sink = WavSink(wav_path, 48000, 2, batch_bytes=8 << 10, opener=opener)
    sink.start()
    _feed_a_lot(sink, 50_000)

    blocker = created[0]
    assert blocker.blocking.wait(timeout=10), "writer never reached the blocked write"

    sink.finish(timeout=0.3)
    assert sink.finish_timed_out is True
    assert sink.finished is False, "a stuck writer must not count as finished"
    assert sink.writer_error is not None
    assert sink.is_silent is False  # not authoritative, and must not pretend to be

    # Let the writer go; the retry must now complete properly.
    blocker.release.set()
    sink.finish(timeout=15)
    assert sink.finished is True
    assert sink.finish_timed_out is False
    assert sink.written_frames > 0

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == sink.written_frames


def test_start_failure_leaves_no_file_behind(wav_path: Path) -> None:
    sink = WavSink(wav_path, 48000, 2, opener=faulty_opener(fail_on_write=1))
    try:
        sink.start()
    except OSError:
        pass
    else:  # pragma: no cover - the opener is rigged to fail
        raise AssertionError("start() should have raised")
    assert not wav_path.exists()
