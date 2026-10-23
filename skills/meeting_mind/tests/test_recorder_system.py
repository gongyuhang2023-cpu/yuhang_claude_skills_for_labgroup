"""Test G-System — SystemAudioRecorder on top of WavSink.

This is the path Teams uses, and the one the 2026-08-03 meeting was recorded
on. Everything here runs against a fake soundcard module, so it exercises the
real threading and teardown without needing an audio endpoint.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from meetingmind.audio import CHANNELS, SAMPLE_RATE, SystemAudioRecorder
from meetingmind.wavsink import _float32_to_int16
from tests.fakes import FakeMicrophone, FakeSoundcardModule
from tests.helpers import make_float32_samples, wait_until


@pytest.fixture
def fake_soundcard(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Install a fake `soundcard` module; the recorder imports it lazily."""

    def _install(
        data: np.ndarray | None = None,
        stall_seconds: float = 0.0,
        open_error: BaseException | None = None,
        name: str = "Fake Loopback (Speakers)",
    ) -> FakeSoundcardModule:
        mic = FakeMicrophone(
            name=name, data=data, stall_seconds=stall_seconds, open_error=open_error
        )
        module = FakeSoundcardModule(mic)
        monkeypatch.setitem(sys.modules, "soundcard", module)
        return module

    return _install


def _stereo(n_frames: int) -> np.ndarray:
    return make_float32_samples(n_frames * CHANNELS).reshape(n_frames, CHANNELS)


def test_records_expected_audio(fake_soundcard, wav_path: Path) -> None:
    frames = 48_000  # one second
    data = _stereo(frames)
    fake_soundcard(data=data)

    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    assert wait_until(lambda: rec.duration_seconds >= 1.0, timeout=15)
    rec.stop()

    assert rec.stop_errors == []
    expected = _float32_to_int16(data.reshape(-1)).tobytes()
    assert wav_path.read_bytes()[44:] == expected

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE
        assert wf.getnchannels() == CHANNELS
        assert wf.getnframes() == frames


def test_duration_reports_what_is_on_disk(fake_soundcard, wav_path: Path) -> None:
    fake_soundcard(data=_stereo(24_000))
    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    assert wait_until(lambda: rec.duration_seconds >= 0.5, timeout=15)
    rec.stop()

    assert rec.duration_seconds == pytest.approx(0.5, abs=1e-6)
    assert wav_path.stat().st_size == 44 + 24_000 * CHANNELS * 2


def test_file_is_playable_while_still_recording(fake_soundcard, wav_path: Path) -> None:
    """The property the incident lacked: stop the world at any moment and the
    audio captured so far is already a valid file."""
    fake_soundcard(data=_stereo(48_000))
    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    assert wait_until(lambda: rec.duration_seconds >= 0.25, timeout=15)

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() >= 12_000

    rec.stop()


def test_device_label_is_recorded(fake_soundcard, wav_path: Path) -> None:
    fake_soundcard(data=_stereo(4_800), name="Realtek Loopback")
    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    rec.stop()
    assert rec.device_label == "Realtek Loopback"


def test_has_signal_is_visible_during_recording(fake_soundcard, wav_path: Path) -> None:
    """session.py reads this ten seconds in, long before stop()."""
    fake_soundcard(data=_stereo(48_000))
    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    assert wait_until(lambda: rec.has_signal, timeout=15)
    rec.stop()


def test_silence_is_only_reported_after_stop(fake_soundcard, wav_path: Path) -> None:
    fake_soundcard(data=np.zeros((48_000, CHANNELS), dtype=np.float32))
    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    assert wait_until(lambda: rec.duration_seconds >= 0.5, timeout=15)
    assert rec.is_silent is False, "must not alarm before the recording is closed"
    rec.stop()
    assert rec.is_silent is True
    assert rec.has_signal is False


def test_start_failure_leaves_no_file(fake_soundcard, wav_path: Path) -> None:
    fake_soundcard(open_error=RuntimeError("device is busy"))
    rec = SystemAudioRecorder(output_path=wav_path)

    with pytest.raises(RuntimeError, match="device is busy"):
        rec.start()

    assert not wav_path.exists(), (
        "a leftover empty WAV would make postprocess think audio exists"
    )


def test_stop_is_idempotent(fake_soundcard, wav_path: Path) -> None:
    fake_soundcard(data=_stereo(4_800))
    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    rec.stop()
    size = wav_path.stat().st_size
    assert rec.stop() == wav_path
    assert rec.stop() == wav_path
    assert wav_path.stat().st_size == size


def test_stop_without_start_is_safe(wav_path: Path) -> None:
    rec = SystemAudioRecorder(output_path=wav_path)
    assert rec.stop() == wav_path
    assert not wav_path.exists()


def test_start_twice_raises(fake_soundcard, wav_path: Path) -> None:
    fake_soundcard(data=_stereo(4_800))
    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    with pytest.raises(RuntimeError, match="already started"):
        rec.start()
    rec.stop()


def test_no_device_label_attribute_on_process_recorder() -> None:
    """`session.py` getattr()s device_label and writes null for process mode.

    Giving AudioRecorder the attribute would silently change what lands in
    metadata.json, so the asymmetry is deliberate and worth pinning.
    """
    from meetingmind.audio import AudioRecorder

    assert not hasattr(AudioRecorder, "device_label")
    assert hasattr(SystemAudioRecorder, "device_label")


@pytest.mark.slow
def test_unjoinable_capture_thread_still_yields_a_complete_file(
    fake_soundcard, wav_path: Path
) -> None:
    """If the capture thread cannot be joined, the recording must survive anyway.

    The stall is longer than the 15-second join, so stop() gives up on the
    thread. The gate then draws the line and the sink closes normally — the
    file must be valid and hold everything captured before the stall.
    """
    frames = 9_600
    data = _stereo(frames)
    module = fake_soundcard(data=data)

    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    assert wait_until(lambda: rec.duration_seconds >= 0.2, timeout=15)

    # Wedge the device: every subsequent record() blocks well past the join,
    # and wait until the thread is genuinely parked inside it — otherwise
    # stop() wins the race and the thread exits cleanly, testing nothing.
    stream = module.mic.stream
    assert stream is not None
    stream._stall_seconds = 30.0
    assert stream.stalling.wait(timeout=10), "capture thread never reached the stall"

    rec.stop()

    assert any("did not stop within 15s" in e for e in rec.stop_errors)
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == frames
    assert wav_path.read_bytes()[44:] == _float32_to_int16(
        data.reshape(-1)
    ).tobytes()
