"""Test B — partial frames must be carried across batches, never truncated per batch.

Capture callbacks hand over whatever the device gave them. If each batch were
independently truncated to whole frames, every batch boundary that landed mid
frame would silently drop samples and shift the channels against each other for
the rest of the recording.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np

from meetingmind.wavsink import WavSink, _float32_to_int16
from tests.helpers import make_float32_samples, to_pcm_bytes


def _f32(*values: float) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def test_frame_split_across_two_chunks_is_reassembled(wav_path: Path) -> None:
    # 1.5 stereo frames delivered as 6 bytes + 6 bytes.
    first = _f32(0.25, -0.25)[:6]          # L + half of R
    second = _f32(0.25, -0.25)[6:] + _f32(0.5)  # rest of R, then an orphan L
    assert len(first) == 6 and len(second) == 6

    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.feed(first)
    sink.feed(second)
    sink.finish()

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == 1
        pcm = wf.readframes(1)

    expected = _float32_to_int16(np.array([0.25, -0.25], dtype=np.float32)).tobytes()
    assert pcm == expected
    # The orphan left channel has nowhere to go and must be reported, not written.
    assert sink.residue_bytes == 4


def test_single_byte_chunks_still_produce_exact_output(wav_path: Path) -> None:
    samples = make_float32_samples(2_000)
    data = to_pcm_bytes(samples)

    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    for i in range(len(data)):
        sink.feed(data[i : i + 1])
    sink.finish()

    expected = _float32_to_int16(samples).tobytes()
    assert wav_path.read_bytes()[44:] == expected
    assert sink.residue_bytes == 0
    # Every one-byte chunk is unaligned; the counter should have noticed.
    assert sink.stats()["unaligned_chunks"] == len(data)


def test_mono_never_has_residue(wav_path: Path) -> None:
    """With one channel a frame is 4 bytes, so any whole sample is a whole frame."""
    samples = make_float32_samples(5_000)
    data = to_pcm_bytes(samples)

    sink = WavSink(wav_path, 16000, 1)
    sink.start()
    for i in range(0, len(data), 4):
        sink.feed(data[i : i + 4])
    sink.finish()

    assert sink.residue_bytes == 0
    assert sink.written_frames == 5_000
    assert wav_path.read_bytes()[44:] == _float32_to_int16(samples).tobytes()


def test_trailing_partial_frame_is_dropped_not_padded(wav_path: Path) -> None:
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.feed(_f32(0.1, 0.2, 0.3))  # 1.5 frames
    sink.finish()

    assert sink.written_frames == 1
    assert sink.residue_bytes == 4
    assert len(wav_path.read_bytes()) == 44 + 4
