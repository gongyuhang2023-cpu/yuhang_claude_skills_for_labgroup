"""Test postprocess with the spark backend — same files out, whichever side ran the ASR."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meetingmind import postprocess
from meetingmind.transcribe import Segment


def _meeting(meeting_dir: Path, mic: bool = False) -> Path:
    meta = {"topic": "t", "date": "2026-10-23", "start_time": "14:00:00",
            "duration_seconds": 400, "recording_status": "complete", "slides": []}
    (meeting_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (meeting_dir / "audio" / "recording.wav").write_bytes(b"RIFF")
    if mic:
        (meeting_dir / "audio" / "mic.wav").write_bytes(b"RIFF")
    return meeting_dir


@pytest.fixture
def spark_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(meeting_dir: Path, files: list[tuple[str, Path]], **kwargs: Any) -> dict:
        calls.append({"files": [(r, p.name) for r, p in files], **kwargs})
        out = {"main": [Segment(0.0, 180.0, "对方说"), Segment(180.0, 360.0, "接着说")]}
        if any(r == "mic" for r, _ in files):
            out["mic"] = [Segment(0.0, 180.0, "我说")]
        return out

    monkeypatch.setattr("meetingmind.remote.transcribe_on_spark", fake)
    return calls


def test_spark_backend_writes_both_files(meeting_dir: Path, spark_calls: list) -> None:
    _meeting(meeting_dir)
    ai_input = postprocess.run(meeting_dir, backend="spark", spark_client=object())

    data = json.loads(ai_input.read_text(encoding="utf-8"))
    assert [s["text"] for s in data["transcript_segments"]] == ["对方说", "接着说"]
    assert all(s["speaker"] is None for s in data["transcript_segments"])
    assert (meeting_dir / "transcript" / "transcript.md").is_file()
    assert spark_calls[0]["files"] == [("main", "recording.wav")]
    assert spark_calls[0]["chunk_minutes"] == postprocess.DEFAULT_CHUNK_MINUTES


def test_two_stream_meeting_is_labelled_and_merged_like_local(meeting_dir: Path,
                                                              spark_calls: list) -> None:
    _meeting(meeting_dir, mic=True)
    ai_input = postprocess.run(meeting_dir, backend="spark", spark_client=object())

    segs = json.loads(ai_input.read_text(encoding="utf-8"))["transcript_segments"]
    assert [(s["speaker"], s["text"]) for s in segs] == [
        ("对方", "对方说"), ("我", "我说"), ("对方", "接着说"),
    ]
    assert spark_calls[0]["files"] == [("main", "recording.wav"), ("mic", "mic.wav")]


def test_spark_backend_refuses_diarize(meeting_dir: Path, spark_calls: list) -> None:
    _meeting(meeting_dir)
    with pytest.raises(RuntimeError, match="--backend local"):
        postprocess.run(meeting_dir, backend="spark", spark_client=object(), diarize=True)
    assert not spark_calls


def test_spark_backend_needs_a_client(meeting_dir: Path) -> None:
    _meeting(meeting_dir)
    with pytest.raises(ValueError, match="spark_client"):
        postprocess.run(meeting_dir, backend="spark")


def test_unknown_backend_is_rejected(meeting_dir: Path) -> None:
    _meeting(meeting_dir)
    with pytest.raises(ValueError):
        postprocess.run(meeting_dir, backend="cloud")
