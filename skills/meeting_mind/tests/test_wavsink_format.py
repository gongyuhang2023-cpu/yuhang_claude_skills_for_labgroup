"""Test A — the output must be byte-identical to what the old code produced.

This is the test that makes the whole refactor safe to ship. If incremental
conversion with a carried residue ever diverges from "join everything, truncate
once, convert once", recordings silently change; nothing downstream would
notice, because a WAV that is subtly wrong still plays.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from meetingmind.wavsink import WavSink
from tests.helpers import (
    chunked,
    legacy_int16_bytes,
    make_float32_samples,
    to_pcm_bytes,
)


@pytest.mark.parametrize(
    ("n_samples", "channels"),
    [
        (200_003, 2),  # odd count -> a half frame is left over at the end
        (200_000, 2),  # exact multiple of the frame size
        (99_999, 1),   # mono, as the mic path records
        (1, 2),        # less than one frame in total
        (0, 2),        # nothing at all
    ],
)
def test_data_matches_legacy_algorithm(
    wav_path: Path, n_samples: int, channels: int
) -> None:
    samples = make_float32_samples(n_samples)
    data = to_pcm_bytes(samples)

    sink = WavSink(wav_path, 48000, channels)
    sink.start()
    for chunk in chunked(data):
        sink.feed(chunk)
    sink.finish()

    assert sink.writer_error is None
    on_disk = wav_path.read_bytes()
    assert on_disk[44:] == legacy_int16_bytes(samples, channels)


def test_header_matches_stdlib_wave(wav_path: Path, tmp_path: Path) -> None:
    samples = make_float32_samples(48_000)
    payload = legacy_int16_bytes(samples, 2)

    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.feed(to_pcm_bytes(samples))
    sink.finish()

    reference = tmp_path / "reference.wav"
    with wave.open(str(reference), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(payload)

    assert wav_path.read_bytes()[:44] == reference.read_bytes()[:44]
    assert wav_path.read_bytes() == reference.read_bytes()


def test_readable_by_stdlib_wave(wav_path: Path) -> None:
    samples = make_float32_samples(96_000)
    sink = WavSink(wav_path, 44100, 2)
    sink.start()
    for chunk in chunked(to_pcm_bytes(samples)):
        sink.feed(chunk)
    sink.finish()

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 44100
        assert wf.getnframes() == 48_000
        assert wf.getnframes() == sink.written_frames


def test_declared_lengths_match_file_size(wav_path: Path) -> None:
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    for chunk in chunked(to_pcm_bytes(make_float32_samples(120_000))):
        sink.feed(chunk)
    sink.finish()

    raw = wav_path.read_bytes()
    riff_declared = int.from_bytes(raw[4:8], "little")
    data_declared = int.from_bytes(raw[40:44], "little")
    assert data_declared == len(raw) - 44
    assert riff_declared == len(raw) - 8
    assert data_declared == sink.written_bytes


def test_header_is_valid_while_still_recording(wav_path: Path) -> None:
    """Mid-recording the file on disk must already be a playable WAV.

    This is the property the incident was missing: at any instant, whatever has
    been captured so far is readable without the process having to survive.
    """
    from tests.helpers import wait_until

    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.feed(to_pcm_bytes(make_float32_samples(48_000)))
    assert wait_until(lambda: sink.written_frames >= 24_000, timeout=5)

    with wave.open(str(wav_path), "rb") as wf:
        frames_now = wf.getnframes()
        assert frames_now >= 24_000
        assert len(wf.readframes(frames_now)) == frames_now * 4

    sink.finish()
