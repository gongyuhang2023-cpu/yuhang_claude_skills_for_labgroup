"""Test J-mic-only — the in-person path: one room microphone, no remote party.

Everything else in this engine assumes there *is* a remote party: `record`
required `--process`, `postprocess` requires `audio/recording.wav`, and the
two-stream speaker labelling is built on "one stream = one voice"
(see `mic.py`'s module docstring). An in-person meeting breaks all three.

The design keeps those three intact instead of special-casing them:
**`MicRecorder` takes over the main audio slot and writes `recording.wav`.**
Since it exposes the same lifecycle as the other two recorders, `stop()` and
`postprocess` need no changes at all — the file name is the *stream slot*, and
`metadata["audio_mode"]` is what says whose voice is inside.

These tests pin exactly that: the slot, the honest metadata, and the fact that
nothing tries to resolve a process that was never given.
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import pytest

from meetingmind.mic import CHANNELS
from meetingmind.session import MeetingSession
from tests.fakes import FakeSoundDeviceModule
from tests.helpers import make_float32_samples, wait_until


@pytest.fixture
def mic_only_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN201
    def _make(frames: int = 44_100, rate: float = 44100.0, topic: str = "in-person"):  # noqa: ANN202
        data = make_float32_samples(frames)
        module = FakeSoundDeviceModule(data=data, default_samplerate=rate)
        monkeypatch.setitem(sys.modules, "sounddevice", module)
        sess = MeetingSession(
            process_keyword=None,          # ← 面对面没有进程可指
            topic=topic,
            output_root=tmp_path,
            capture_slides=False,
            mic_only=True,
        )
        return sess, module

    return _make


def _run(sess: MeetingSession) -> dict:
    sess.start()
    wait_until(lambda: sess.meeting_dir is not None, timeout=5)
    summary = sess.stop()
    meta_path = Path(summary["meeting_dir"]) / "metadata.json"
    return json.loads(meta_path.read_text(encoding="utf-8"))


def test_mic_lands_in_the_single_stream_slot(mic_only_session) -> None:
    """**recording.wav，不是 mic.wav。**

    这是整个设计的支点：写进单路槽位，`postprocess` 就一行都不用改
    （它找 recording.wav，没有 mic.wav 就走它现成的单路径）。
    改成写 mic.wav 的话，postprocess 会直接 FileNotFoundError。
    """
    sess, _ = mic_only_session()
    meta = _run(sess)
    d = Path(meta["meeting_dir"]) if "meeting_dir" in meta else sess.meeting_dir

    assert (d / "audio" / "recording.wav").is_file()
    assert not (d / "audio" / "mic.wav").exists(), "mic-only 不该有第二路"
    assert meta["audio_path"] == "audio/recording.wav"
    assert meta["mic_path"] is None


def test_metadata_tells_the_truth_about_the_source(mic_only_session) -> None:
    """槽位是共用的，所以 metadata 必须说清里面是谁的声音、什么格式。

    采样率和声道数原本是**写死的 48000/2**（loopback 恒定如此）。mic 走设备
    原生采样率（实测 44100）、单声道 —— 写死会让 metadata 说谎，而下游
    （转录、分离）都读它。
    """
    sess, _ = mic_only_session(rate=44100.0)
    meta = _run(sess)

    assert meta["audio_mode"] == "mic"
    assert meta["audio_sample_rate"] == 44100
    assert meta["audio_channels"] == CHANNELS == 1
    assert meta["parameters"]["mic_only"] is True
    assert meta["parameters"]["mic"] is False, "mic-only 不走第二路那个开关"


def test_no_process_resolution_is_attempted(mic_only_session) -> None:
    """没给进程就不该去解进程 —— 解了会在 `_resolve_pid()` 里抛。"""
    sess, _ = mic_only_session()
    meta = _run(sess)

    assert meta["process_keyword"] is None
    assert meta["pid"] is None
    assert meta["window_keyword"] is None, "没有进程可推窗口标题"


def test_wav_is_mono_at_device_rate(mic_only_session) -> None:
    sess, _ = mic_only_session(frames=44_100, rate=44100.0)
    _run(sess)
    wav_path = sess.meeting_dir / "audio" / "recording.wav"

    with wave.open(str(wav_path)) as w:
        assert w.getnchannels() == CHANNELS
        assert w.getframerate() == 44100
        assert w.getnframes() > 0


def test_device_rate_is_honoured_not_hardcoded(mic_only_session) -> None:
    """换一支报 48 kHz 的麦，metadata 要跟着变 —— 不是恰好和 loopback 撞上。"""
    sess, _ = mic_only_session(frames=48_000, rate=48000.0)
    meta = _run(sess)
    assert meta["audio_sample_rate"] == 48000
    assert meta["audio_mode"] == "mic", "别把它误判成 loopback"
