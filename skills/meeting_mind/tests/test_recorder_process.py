"""Test G-Process — AudioRecorder on top of WavSink.

The distinguishing feature of this path is that its producer cannot be stopped
on demand: ProcTap's `stop()` waits one second for its worker and then drops
the reference whether or not the thread exited. So the tests here are less
about "does it record" and more about "what happens when the producer will not
go away".
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np
import pytest

from meetingmind.audio import CHANNELS, SAMPLE_RATE, AudioRecorder
from meetingmind.wavsink import _float32_to_int16
from tests.fakes import proctap_factory
from tests.helpers import make_float32_samples, to_pcm_bytes, wait_until


@pytest.fixture
def fake_proctap(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    def _install(**kwargs):  # noqa: ANN003, ANN202
        factory = proctap_factory(**kwargs)
        monkeypatch.setattr("meetingmind.audio.ProcessAudioCapture", factory)
        return factory

    return _install


def test_records_expected_audio(fake_proctap, wav_path: Path) -> None:
    samples = make_float32_samples(48_000 * CHANNELS)
    factory = fake_proctap(data=to_pcm_bytes(samples))

    rec = AudioRecorder(pid=1234, output_path=wav_path)
    rec.start()
    assert factory.created[0].exhausted.wait(timeout=15)
    assert wait_until(lambda: rec.duration_seconds >= 1.0, timeout=15)
    rec.stop()

    assert rec.stop_errors == []
    assert wav_path.read_bytes()[44:] == _float32_to_int16(samples).tobytes()
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE
        assert wf.getnchannels() == CHANNELS


def test_late_callbacks_after_stop_are_rejected_and_counted(
    fake_proctap, wav_path: Path
) -> None:
    """The incident's shape: a producer that keeps going after stop() returns.

    Before this refactor those deliveries landed in a list that was about to
    be discarded — silently. Now they are refused at the gate, counted, and
    reported, and the file they arrive too late for is already closed and
    correct.
    """
    samples = make_float32_samples(9_600 * CHANNELS)
    factory = fake_proctap(data=to_pcm_bytes(samples), late_seconds=0.6)

    rec = AudioRecorder(pid=1234, output_path=wav_path)
    rec.start()
    tap = factory.created[0]
    assert tap.exhausted.wait(timeout=15)
    assert wait_until(lambda: rec.duration_seconds >= 0.2, timeout=15)

    expected = _float32_to_int16(samples).tobytes()
    rec.stop()
    digest_after_stop = hashlib.sha256(wav_path.read_bytes()).hexdigest()

    # Let the rogue worker run itself out, then check nothing it sent got in.
    assert tap.worker_finished.wait(timeout=15)
    assert tap.late_deliveries > 0, "the fake never actually delivered late"

    # Not every one of the fake's post-exhaustion deliveries counts as "late":
    # the ones that landed before the gate closed arrived while the recording
    # was still running and were legitimately kept. The rest were refused.
    late_calls = rec.sink_stats["late_calls"]
    assert 0 < late_calls <= tap.late_deliveries
    assert rec.sink_stats["late_bytes"] > 0

    # The two properties that actually matter:
    raw = wav_path.read_bytes()
    assert raw[44 : 44 + len(expected)] == expected  # the real audio is intact
    assert hashlib.sha256(raw).hexdigest() == digest_after_stop  # and frozen


def test_file_is_playable_mid_recording(fake_proctap, wav_path: Path) -> None:
    factory = fake_proctap(data=to_pcm_bytes(make_float32_samples(48_000 * CHANNELS)))
    rec = AudioRecorder(pid=1234, output_path=wav_path)
    rec.start()
    assert factory.created[0].exhausted.wait(timeout=15)
    assert wait_until(lambda: rec.duration_seconds >= 0.5, timeout=15)

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() >= 24_000

    rec.stop()


def test_start_failure_leaves_no_file(fake_proctap, wav_path: Path) -> None:
    fake_proctap(start_error=RuntimeError("PID 1234 has no audio session"))
    rec = AudioRecorder(pid=1234, output_path=wav_path)

    with pytest.raises(RuntimeError, match="no audio session"):
        rec.start()

    assert not wav_path.exists()


def test_teardown_errors_do_not_lose_the_recording(
    fake_proctap, wav_path: Path
) -> None:
    samples = make_float32_samples(4_800 * CHANNELS)
    factory = fake_proctap(
        data=to_pcm_bytes(samples), stop_error=RuntimeError("COM went away")
    )
    rec = AudioRecorder(pid=1234, output_path=wav_path)
    rec.start()
    assert factory.created[0].exhausted.wait(timeout=15)
    assert wait_until(lambda: rec.duration_seconds >= 0.1, timeout=15)
    rec.stop()

    assert any("COM went away" in e for e in rec.stop_errors)
    assert wav_path.read_bytes()[44:] == _float32_to_int16(samples).tobytes()


def test_silence_only_after_stop(fake_proctap, wav_path: Path) -> None:
    zeros = np.zeros(48_000 * CHANNELS, dtype=np.float32)
    factory = fake_proctap(data=to_pcm_bytes(zeros))
    rec = AudioRecorder(pid=1234, output_path=wav_path)
    rec.start()
    assert factory.created[0].exhausted.wait(timeout=15)
    assert wait_until(lambda: rec.duration_seconds >= 1.0, timeout=15)
    assert rec.is_silent is False
    rec.stop()
    assert rec.is_silent is True
    assert rec.has_signal is False


def test_stop_is_idempotent_and_start_twice_raises(
    fake_proctap, wav_path: Path
) -> None:
    fake_proctap(data=to_pcm_bytes(make_float32_samples(4_800 * CHANNELS)))
    rec = AudioRecorder(pid=1234, output_path=wav_path)
    rec.start()
    with pytest.raises(RuntimeError, match="already started"):
        rec.start()
    rec.stop()
    size = wav_path.stat().st_size
    rec.stop()
    assert wav_path.stat().st_size == size


def test_stop_without_start_is_safe(wav_path: Path) -> None:
    rec = AudioRecorder(pid=1234, output_path=wav_path)
    assert rec.stop() == wav_path
    assert not wav_path.exists()


def test_context_manager_writes_on_exception(fake_proctap, wav_path: Path) -> None:
    samples = make_float32_samples(4_800 * CHANNELS)
    factory = fake_proctap(data=to_pcm_bytes(samples))

    with pytest.raises(ValueError, match="boom"):  # noqa: SIM117
        with AudioRecorder(pid=1234, output_path=wav_path) as rec:
            assert factory.created[0].exhausted.wait(timeout=15)
            assert wait_until(lambda: rec.duration_seconds >= 0.1, timeout=15)
            raise ValueError("boom")

    assert wav_path.read_bytes()[44:] == _float32_to_int16(samples).tobytes()
