"""Stopping again must actually retry a sink whose finish() timed out.

WavSink stays retryable after a timeout, but by then the recorder has already
dropped its producer reference — so without an explicit retry path, a second
`stop()` returns early and the sink is never settled, no matter how long the
writer has since been free. The session then reports a meeting as complete on
figures that were never computed.
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import pytest

from meetingmind.audio import CHANNELS, SystemAudioRecorder
from meetingmind.session import MeetingSession
from meetingmind.wavsink import WavSink
from tests.fakes import FakeMicrophone, FakeSoundcardModule, blocking_write_opener
from tests.helpers import make_float32_samples, to_pcm_bytes, wait_until


def _stereo(n_frames: int):  # noqa: ANN202
    return make_float32_samples(n_frames * CHANNELS).reshape(n_frames, CHANNELS)


@pytest.mark.slow  # stop() waits out WavSink's default 30s finish timeout
def test_second_stop_settles_a_timed_out_sink(
    monkeypatch: pytest.MonkeyPatch, wav_path: Path
) -> None:
    opener, created = blocking_write_opener(block_on_write=4)

    real_wavsink = WavSink

    def blocking_sink(*args: object, **kwargs: object) -> WavSink:
        kwargs["opener"] = opener
        kwargs["batch_bytes"] = 8 << 10
        return real_wavsink(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("meetingmind.audio.WavSink", blocking_sink)
    monkeypatch.setitem(
        sys.modules, "soundcard", FakeSoundcardModule(FakeMicrophone(data=_stereo(24_000)))
    )

    rec = SystemAudioRecorder(output_path=wav_path)
    rec.start()
    blocker = created[0]
    assert blocker.blocking.wait(timeout=15), "writer never reached the blocked write"

    sink = rec._sink
    assert sink is not None
    sink.finish(timeout=0.3)  # simulate the timeout stop() would hit
    assert sink.finished is False
    assert sink.finish_timed_out is True

    rec.stop()  # clears the producer reference; sink still stuck
    assert sink.finished is False

    blocker.release.set()
    rec.stop()  # the retry that used to be a no-op

    assert sink.finished is True
    assert rec.duration_seconds > 0
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == sink.written_frames


@pytest.mark.slow  # ditto: session.stop() sits through the stuck sink's timeout
def test_session_refuses_to_call_an_unsettled_recording_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """metadata must say `incomplete`, so postprocess sends the user to recover."""
    opener, created = blocking_write_opener(block_on_write=4)
    real_wavsink = WavSink

    def blocking_sink(*args: object, **kwargs: object) -> WavSink:
        kwargs["opener"] = opener
        kwargs["batch_bytes"] = 8 << 10
        return real_wavsink(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("meetingmind.audio.WavSink", blocking_sink)
    monkeypatch.setitem(
        sys.modules, "soundcard", FakeSoundcardModule(FakeMicrophone(data=_stereo(24_000)))
    )

    sess = MeetingSession(
        process_keyword="teams",
        topic="stuck-writer",
        output_root=tmp_path,
        interval=5,
        threshold=5,
        capture_slides=False,
        system_audio=True,
    )
    sess.start()
    blocker = created[0]
    assert blocker.blocking.wait(timeout=15)

    assert sess._recorder is not None
    sess._recorder._sink.finish(timeout=0.3)  # type: ignore[union-attr]
    sess.stop()

    meta = json.loads((sess.meeting_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["recording_status"] == "incomplete"

    from meetingmind.postprocess import _require_finished_recording

    with pytest.raises(RuntimeError, match="did not finish cleanly"):
        _require_finished_recording(sess.meeting_dir, meta)

    blocker.release.set()


def test_normal_stop_still_reports_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unhappy path must not have made the happy path pessimistic."""
    monkeypatch.setitem(
        sys.modules, "soundcard", FakeSoundcardModule(FakeMicrophone(data=_stereo(24_000)))
    )
    sess = MeetingSession(
        process_keyword="teams",
        topic="healthy",
        output_root=tmp_path,
        interval=5,
        threshold=5,
        capture_slides=False,
        system_audio=True,
    )
    sess.start()
    assert wait_until(lambda: sess._recorder.duration_seconds >= 0.4, timeout=15)
    sess.stop()

    meta = json.loads((sess.meeting_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["recording_status"] == "complete"
    assert meta["audio_stats"]["finish_timed_out"] is False


def test_sink_feed_still_safe_while_unsettled(wav_path: Path) -> None:
    """Feeding a timed-out sink must stay harmless, as it is in every other state."""
    opener, created = blocking_write_opener(block_on_write=4)
    sink = WavSink(wav_path, 48000, 2, batch_bytes=8 << 10, opener=opener)
    sink.start()
    for _ in range(40):
        sink.feed(to_pcm_bytes(make_float32_samples(4_800 * 2)))

    blocker = created[0]
    assert blocker.blocking.wait(timeout=15)
    sink.finish(timeout=0.3)

    for _ in range(20):
        sink.feed(to_pcm_bytes(make_float32_samples(1_000)))  # must not raise

    blocker.release.set()
    sink.finish(timeout=15)
    assert sink.finished is True
