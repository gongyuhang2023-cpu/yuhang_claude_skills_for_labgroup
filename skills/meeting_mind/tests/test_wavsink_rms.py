"""Test C — silence detection must accumulate from the original float32.

The old code computed RMS over the whole recording at once. The replacement
accumulates per batch, which is only equivalent if it accumulates in float64
over the *input* samples. Accumulating over the int16 output instead would
quantise anything below one LSB to zero and report a quiet-but-real recording
as "COMPLETELY SILENT" — a warning the operator would act on.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from meetingmind.wavsink import WavSink
from tests.helpers import chunked, make_float32_samples, to_pcm_bytes


def _run(path: Path, samples: np.ndarray, channels: int = 2) -> WavSink:
    sink = WavSink(path, 48000, channels)
    sink.start()
    for chunk in chunked(to_pcm_bytes(samples)):
        sink.feed(chunk)
    sink.finish()
    return sink


def test_all_zero_is_silent(wav_path: Path) -> None:
    sink = _run(wav_path, np.zeros(48_000, dtype=np.float32))
    assert sink.rms == 0.0
    assert sink.is_silent is True


def test_empty_recording_reports_silent(wav_path: Path) -> None:
    """Matches the previous behaviour: no samples at all counted as silent."""
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.finish()
    assert sink.rms == 0.0
    assert sink.is_silent is True


def test_sub_lsb_signal_is_silent_but_not_zero(wav_path: Path) -> None:
    """1e-8 quantises to int16 zero, yet the RMS must still see it.

    This is the test that fails if anyone "simplifies" the accumulator to run
    over the converted output.
    """
    value = np.float32(1e-8)
    sink = _run(wav_path, np.full(48_000, value, dtype=np.float32))

    assert wav_path.read_bytes()[44:] == b"\x00" * (48_000 * 2)
    assert sink.rms == pytest.approx(float(value), rel=1e-6)
    assert sink.rms > 0.0
    assert sink.is_silent is True  # below the 1e-7 threshold


def test_quiet_signal_above_threshold_is_not_silent(wav_path: Path) -> None:
    value = np.float32(1e-6)
    sink = _run(wav_path, np.full(48_000, value, dtype=np.float32))
    assert sink.rms == pytest.approx(float(value), rel=1e-6)
    assert sink.is_silent is False


def test_rms_matches_float64_reference(wav_path: Path) -> None:
    samples = make_float32_samples(200_002)
    sink = _run(wav_path, samples)

    reference = math.sqrt(float(np.mean(samples.astype(np.float64) ** 2)))
    assert sink.rms == pytest.approx(reference, rel=1e-12)
    assert sink.is_silent is False


def test_is_silent_is_false_before_finish(wav_path: Path) -> None:
    """Callers read is_silent only after stop(); before that it must not alarm."""
    sink = WavSink(wav_path, 48000, 2)
    sink.start()
    sink.feed(to_pcm_bytes(np.zeros(4_800, dtype=np.float32)))
    assert sink.is_silent is False
    sink.finish()
    assert sink.is_silent is True


def test_rms_ignores_the_dropped_trailing_partial_frame(wav_path: Path) -> None:
    """Only whole frames are converted, so only whole frames feed the RMS."""
    samples = np.array([0.5, 0.5, 0.5], dtype=np.float32)  # 1.5 stereo frames
    sink = _run(wav_path, samples)
    assert sink.rms == pytest.approx(0.5, rel=1e-12)
    assert sink.written_frames == 1
