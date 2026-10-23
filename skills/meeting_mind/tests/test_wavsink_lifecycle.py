"""Lifecycle contract: idempotent finish, clean rollback, honest stats."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from meetingmind.wavsink import WavSink
from tests.helpers import make_float32_samples, to_pcm_bytes, wait_until


def test_start_twice_raises(wav_path: Path) -> None:
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    with pytest.raises(RuntimeError, match="already started"):
        sink.start()
    sink.finish()


def test_finish_is_idempotent(wav_path: Path) -> None:
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.feed(to_pcm_bytes(make_float32_samples(4_800)))
    sink.finish()
    size = wav_path.stat().st_size
    frames = sink.written_frames

    sink.finish()
    sink.finish()
    assert wav_path.stat().st_size == size
    assert sink.written_frames == frames


def test_finish_without_start_is_safe(wav_path: Path) -> None:
    sink = WavSink(wav_path, 48000, 2)
    sink.finish()
    assert sink.finished is True
    assert not wav_path.exists()


def test_abort_removes_the_file(wav_path: Path) -> None:
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    assert wav_path.exists()
    sink.abort()
    assert not wav_path.exists()


def test_abort_without_start_is_safe(wav_path: Path) -> None:
    WavSink(wav_path, 48000, 2).abort()
    assert not wav_path.exists()


def test_rejects_nonsense_format() -> None:
    with pytest.raises(ValueError):
        WavSink(Path("x.wav"), 0, 2)
    with pytest.raises(ValueError):
        WavSink(Path("x.wav"), 48000, 0)


def test_creates_missing_parent_directory(wav_path: Path) -> None:
    assert not wav_path.parent.exists()
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    assert wav_path.parent.is_dir()
    sink.finish()


def test_concurrent_feeders_are_all_written(wav_path: Path) -> None:
    """Several capture threads may share one sink (loopback + mic mixes never
    do today, but nothing in the contract forbids it and the lock must hold)."""
    sink = WavSink(wav_path, 48000, 2)
    sink.start()

    block = to_pcm_bytes(make_float32_samples(480 * 2))
    per_thread = 50
    n_threads = 8

    def worker() -> None:
        for _ in range(per_thread):
            sink.feed(block)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    sink.finish()

    assert sink.writer_error is None
    assert sink.dropped_bytes == 0
    expected_frames = n_threads * per_thread * 480
    assert sink.written_frames == expected_frames
    assert wav_path.stat().st_size == 44 + expected_frames * 4


def test_written_seconds_tracks_the_file_not_the_queue(wav_path: Path) -> None:
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.feed(to_pcm_bytes(make_float32_samples(48_000 * 2)))  # 1 second
    assert wait_until(lambda: sink.written_frames == 48_000, timeout=10)
    assert sink.written_seconds == pytest.approx(1.0)
    sink.finish()
    assert sink.written_seconds == pytest.approx(1.0)


def test_stats_are_json_friendly(wav_path: Path) -> None:
    import json

    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.feed(to_pcm_bytes(make_float32_samples(4_800)))
    sink.finish()
    json.dumps(sink.stats())  # metadata.json embeds this verbatim
