"""Test I-session — metadata is published early, written atomically, and honest.

Also the end-to-end simulation of the 2026-08-03 incident's software path:
WGC reports the window gone → `_on_window_lost()` → `stop()`. Everything but
the real WGC and the real sound card is exercised here.
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from meetingmind.audio import CHANNELS
from meetingmind.session import MeetingSession, _warn
from meetingmind.wavsink import _float32_to_int16
from tests.fakes import FakeMicrophone, FakeSoundcardModule
from tests.helpers import make_float32_samples, wait_until


def _stereo(n_frames: int) -> np.ndarray:
    return make_float32_samples(n_frames * CHANNELS).reshape(n_frames, CHANNELS)


@pytest.fixture
def audio_only_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN201
    """A system-audio, no-slides session backed by a fake sound card."""

    def _make(frames: int = 24_000, topic: str = "unit-test"):  # noqa: ANN202
        data = _stereo(frames)
        mic = FakeMicrophone(data=data)
        monkeypatch.setitem(sys.modules, "soundcard", FakeSoundcardModule(mic))
        sess = MeetingSession(
            process_keyword="teams",
            topic=topic,
            output_root=tmp_path,
            interval=5,
            threshold=5,
            capture_slides=False,
            system_audio=True,
        )
        return sess, data

    return _make


def _read_metadata(sess: MeetingSession) -> dict:
    return json.loads((sess.meeting_dir / "metadata.json").read_text(encoding="utf-8"))


def test_metadata_exists_from_the_moment_recording_starts(audio_only_session) -> None:
    sess, _ = audio_only_session()
    sess.start()
    try:
        meta = _read_metadata(sess)
        assert meta["recording_status"] == "recording"
        # Must be a real clock string: postprocess feeds it to strptime and
        # only catches ValueError, so null would be a TypeError crash.
        assert isinstance(meta["start_time"], str)
        assert len(meta["start_time"].split(":")) == 3
        assert meta["slides"] == []
    finally:
        sess.stop()


def test_final_metadata_is_marked_complete(audio_only_session) -> None:
    sess, _ = audio_only_session()
    sess.start()
    assert wait_until(lambda: sess._recorder.duration_seconds >= 0.5, timeout=15)
    sess.stop()

    meta = _read_metadata(sess)
    assert meta["recording_status"] == "complete"
    assert meta["audio_duration_seconds"] >= 0.5
    assert meta["end_time"] >= meta["start_time"]


def test_metadata_keys_are_a_superset_of_the_old_schema(audio_only_session) -> None:
    """Downstream reads metadata by key; nothing may quietly disappear."""
    sess, _ = audio_only_session()
    sess.start()
    sess.stop()

    legacy_keys = {
        "date", "start_time", "end_time", "duration_seconds", "topic",
        "process_keyword", "window_keyword", "pid", "pid_attributed",
        "audio_mode", "audio_device", "audio_path", "audio_sample_rate",
        "audio_channels", "audio_duration_seconds", "mic_path", "mic_captured",
        "mic_duration_seconds", "slides_count", "revisits_count", "slides",
        "parameters",
    }
    meta = _read_metadata(sess)
    assert legacy_keys <= set(meta)
    assert {"recording_status", "audio_stats", "mic_stats"} <= set(meta)


def test_metadata_records_writer_counters(audio_only_session) -> None:
    sess, _ = audio_only_session()
    sess.start()
    assert wait_until(lambda: sess._recorder.duration_seconds >= 0.5, timeout=15)
    sess.stop()

    stats = _read_metadata(sess)["audio_stats"]
    assert stats["written_frames"] > 0
    assert stats["dropped_bytes"] == 0
    assert stats["writer_error"] is None
    assert stats["late_calls"] == 0


def test_window_lost_still_produces_audio_and_metadata(audio_only_session) -> None:
    """The incident, end to end, minus the two things only hardware can prove.

    WGC used to report the window gone, stop() would run, and the audio never
    reached disk because it was only written at the very end. Now the file is
    already there and stop() merely finishes it.
    """
    sess, data = audio_only_session(frames=24_000, topic="window-lost")
    sess.start()
    assert wait_until(lambda: sess._recorder.duration_seconds >= 0.5, timeout=15)

    # Exactly what SlideCapture's on_closed callback does.
    sess._on_window_lost()
    sess.wait()  # returns immediately: request_stop() was called
    sess.stop()

    wav = sess.meeting_dir / "audio" / "recording.wav"
    assert wav.is_file()
    with wave.open(str(wav), "rb") as wf:
        assert wf.getnframes() == 24_000
    assert wav.read_bytes()[44:] == _float32_to_int16(data.reshape(-1)).tobytes()

    meta = _read_metadata(sess)
    assert meta["recording_status"] == "complete"
    assert meta["audio_duration_seconds"] == pytest.approx(0.5, abs=1e-6)


def test_audio_survives_a_crash_before_stop(audio_only_session) -> None:
    """Abandon the session mid-recording: the WAV and metadata are still there.

    This is the shape of the loss — a process that never reaches stop(). What
    it leaves behind is now a directory `recover` can finish, not an orphan.
    """
    sess, _ = audio_only_session(frames=48_000)
    sess.start()
    assert wait_until(lambda: sess._recorder.duration_seconds >= 0.5, timeout=15)

    wav = sess.meeting_dir / "audio" / "recording.wav"
    with wave.open(str(wav), "rb") as wf:
        assert wf.getnframes() >= 24_000
    assert _read_metadata(sess)["recording_status"] == "recording"

    sess.stop()  # tidy up the fixture's threads


def test_metadata_write_is_atomic(audio_only_session, monkeypatch) -> None:
    """A failed write must leave the previous metadata intact, not truncated."""
    sess, _ = audio_only_session()
    sess.start()
    before = (sess.meeting_dir / "metadata.json").read_text(encoding="utf-8")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("meetingmind.session.os.replace", boom)
    with pytest.raises(RuntimeError, match="could not update"):
        sess._write_metadata_file(sess._build_metadata(status="complete"))

    assert (sess.meeting_dir / "metadata.json").read_text(encoding="utf-8") == before
    leftovers = list(sess.meeting_dir.glob(".metadata.json.tmp-*"))
    assert leftovers == [], f"temp files left behind: {leftovers}"

    monkeypatch.undo()
    sess.stop()


def test_warn_survives_an_unencodable_console(capsys, monkeypatch) -> None:
    """A warning must never be able to abort its caller.

    Found the hard way: session's silence warning contains '⚠', a GBK console
    raised UnicodeEncodeError out of stop(), and the metadata write that came
    next never happened — a silent recording became a recording with no
    metadata.
    """

    class GbkStderr:
        def write(self, text: str) -> int:
            text.encode("gbk")  # raises on the warning sign
            return len(text)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(sys, "stderr", GbkStderr())
    _warn("\n⚠️  [session] 完全静音\n")  # must not raise
