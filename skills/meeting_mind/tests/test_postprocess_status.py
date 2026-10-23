"""Test I-postprocess — refuse unfinished recordings, never touch old ones."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meetingmind.postprocess import _require_finished_recording


def _meta(**kwargs: object) -> dict:
    base = {"date": "2026-08-04", "start_time": "14:00:00", "slides": []}
    base.update(kwargs)
    return base


def test_missing_key_is_treated_as_complete(tmp_path: Path) -> None:
    """Every meeting recorded before this field existed must still process.

    Getting this default wrong would lock the user's whole archive out of
    postprocessing — the single most damaging way this change could go wrong.
    """
    _require_finished_recording(tmp_path, _meta())  # must not raise


def test_complete_and_recovered_pass(tmp_path: Path) -> None:
    _require_finished_recording(tmp_path, _meta(recording_status="complete"))
    _require_finished_recording(tmp_path, _meta(recording_status="recovered"))


def test_recording_status_is_refused_with_a_usable_hint(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        _require_finished_recording(tmp_path, _meta(recording_status="recording"))

    message = str(excinfo.value)
    assert "recover" in message
    assert str(tmp_path) in message, "the error must name the directory to fix"


def test_unknown_status_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="did not finish cleanly"):
        _require_finished_recording(tmp_path, _meta(recording_status="weird"))


def test_run_refuses_before_touching_the_audio(tmp_path: Path) -> None:
    """The gate must come first: no model load, no transcription, no writes."""
    from meetingmind.postprocess import run

    meeting = tmp_path / "2026-08-04-unfinished"
    (meeting / "audio").mkdir(parents=True)
    (meeting / "audio" / "recording.wav").write_bytes(b"RIFF" + b"\x00" * 40)
    (meeting / "metadata.json").write_text(
        json.dumps(_meta(recording_status="recording")), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="did not finish cleanly"):
        run(meeting)

    assert not (meeting / "ai_input.json").exists()
    assert not (meeting / "transcript").exists()
