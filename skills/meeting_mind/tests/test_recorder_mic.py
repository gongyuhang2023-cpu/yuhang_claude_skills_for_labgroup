"""Test G-Mic — MicRecorder on top of WavSink.

The mic path is the one that differs structurally: mono, a sample rate that is
only known at start(), and a real-time PortAudio callback whose buffer stops
being valid the instant it returns.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from meetingmind.mic import CHANNELS, MicRecorder
from meetingmind.wavsink import _float32_to_int16
from tests.fakes import FakeSoundDeviceModule
from tests.helpers import make_float32_samples, wait_until


@pytest.fixture
def fake_sounddevice(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    def _install(**kwargs) -> FakeSoundDeviceModule:  # noqa: ANN003
        module = FakeSoundDeviceModule(**kwargs)
        monkeypatch.setitem(sys.modules, "sounddevice", module)
        return module

    return _install


def test_records_mono_at_device_rate(fake_sounddevice, wav_path: Path) -> None:
    data = make_float32_samples(44_100)
    module = fake_sounddevice(data=data, default_samplerate=44100.0)

    rec = MicRecorder(output_path=wav_path)
    rec.start()
    assert rec.samplerate == 44100
    assert module.stream is not None
    assert module.stream.exhausted.wait(timeout=15)
    assert wait_until(lambda: rec.duration_seconds >= 1.0, timeout=15)
    rec.stop()

    assert rec.stop_errors == []
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnchannels() == CHANNELS == 1
        assert wf.getframerate() == 44100
        assert wf.getnframes() == 44_100


def test_callback_buffer_is_copied_not_referenced(
    fake_sounddevice, wav_path: Path
) -> None:
    """PortAudio reuses `indata`; keeping a view of it would write garbage.

    The fake overwrites the buffer with NaN right after each callback returns,
    so a missing copy shows up as a corrupted file rather than as luck.
    """
    data = make_float32_samples(8_192)
    module = fake_sounddevice(data=data, default_samplerate=16000.0, blocksize=512)

    rec = MicRecorder(output_path=wav_path)
    rec.start()
    assert module.stream is not None
    assert module.stream.exhausted.wait(timeout=15)
    assert wait_until(lambda: rec.duration_seconds >= 8_192 / 16000, timeout=15)
    rec.stop()

    # Prove the hazard was actually staged — otherwise this test would pass
    # for the wrong reason on a fake that quietly stopped clobbering.
    assert module.stream.overwrites >= 8_192 // 512
    assert wav_path.read_bytes()[44:] == _float32_to_int16(data).tobytes()


def test_falls_back_when_the_device_will_not_report_a_rate(
    fake_sounddevice, wav_path: Path
) -> None:
    fake_sounddevice(
        data=make_float32_samples(1_600),
        query_error=RuntimeError("no such device"),
    )
    rec = MicRecorder(output_path=wav_path)
    rec.start()
    rec.stop()

    assert rec.samplerate == 16000
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == 16000


def test_explicit_samplerate_skips_probing(fake_sounddevice, wav_path: Path) -> None:
    fake_sounddevice(
        data=make_float32_samples(4_800),
        query_error=AssertionError("query_devices must not be called"),
    )
    rec = MicRecorder(output_path=wav_path, samplerate=48000)
    rec.start()
    rec.stop()
    assert rec.samplerate == 48000


def test_start_failure_leaves_no_file(fake_sounddevice, wav_path: Path) -> None:
    fake_sounddevice(start_error=RuntimeError("microphone in use"))
    rec = MicRecorder(output_path=wav_path)

    with pytest.raises(RuntimeError, match="microphone in use"):
        rec.start()

    assert not wav_path.exists()


def test_teardown_errors_do_not_lose_the_recording(
    fake_sounddevice, wav_path: Path
) -> None:
    data = make_float32_samples(3_200)
    module = fake_sounddevice(
        data=data,
        default_samplerate=16000.0,
        stop_error=RuntimeError("PortAudio hiccup"),
    )
    rec = MicRecorder(output_path=wav_path)
    rec.start()
    assert module.stream is not None
    assert module.stream.exhausted.wait(timeout=15)
    assert wait_until(lambda: rec.duration_seconds >= 0.2, timeout=15)
    rec.stop()

    assert any("PortAudio hiccup" in e for e in rec.stop_errors)
    assert wav_path.read_bytes()[44:] == _float32_to_int16(data).tobytes()


def test_silence_only_after_stop(fake_sounddevice, wav_path: Path) -> None:
    module = fake_sounddevice(
        data=np.zeros(16_000, dtype=np.float32), default_samplerate=16000.0
    )
    rec = MicRecorder(output_path=wav_path)
    rec.start()
    assert module.stream is not None
    assert module.stream.exhausted.wait(timeout=15)
    assert wait_until(lambda: rec.duration_seconds >= 1.0, timeout=15)
    assert rec.is_silent is False
    rec.stop()
    assert rec.is_silent is True
    assert rec.has_signal is False


def test_stop_is_idempotent_and_start_twice_raises(
    fake_sounddevice, wav_path: Path
) -> None:
    fake_sounddevice(data=make_float32_samples(1_600))
    rec = MicRecorder(output_path=wav_path)
    rec.start()
    with pytest.raises(RuntimeError, match="already started"):
        rec.start()
    rec.stop()
    size = wav_path.stat().st_size
    rec.stop()
    assert wav_path.stat().st_size == size


def test_stop_without_start_is_safe(wav_path: Path) -> None:
    rec = MicRecorder(output_path=wav_path)
    assert rec.stop() == wav_path
    assert not wav_path.exists()


def test_mic_recorder_has_no_device_label() -> None:
    """Only SystemAudioRecorder carries one; metadata depends on that."""
    assert not hasattr(MicRecorder, "device_label")
    assert hasattr(MicRecorder, "samplerate")
