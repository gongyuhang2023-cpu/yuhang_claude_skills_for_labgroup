"""End-to-end postprocess: meeting_dir → transcript.md + ai_input.json.

The `postprocess` step bridges Phase 1's recording output (audio + slide
screenshots + metadata.json) and Phase 3's AI summary. It runs ASR if
needed and emits ``ai_input.json`` — a Claude-readable structured file
that combines metadata, slide index, and transcript segments in one place.

Pipeline (top-down in `run()`):

  1. Load `<meeting_dir>/metadata.json` (raises FileNotFoundError if absent).
  2. If `transcript/transcript.md` exists AND `--force` is not set, skip
     transcription — assume a prior successful run. `ai_input.json` must
     also exist; if it's gone, raise so the user can `--force` to recover.
  3. Otherwise, run Qwen3-ASR over `audio/recording.wav`, assemble the
     `ai_input.json` payload, and write both files in this order:

       a. `ai_input.json`   (machine-readable, written first)
       b. `transcript.md`   (human-readable, written last)

     `transcript.md` is the idempotency marker — it's written last so a
     crash between steps a and b leaves no `transcript.md`, causing the
     next run to retry cleanly instead of trusting partial state.

Public API:
  - `run(meeting_dir, ...)` — main entry point.
  - `load_metadata(meeting_dir)` — read `metadata.json` as a dict.
  - `build_ai_input(metadata, segments)` — pure transform to the
    ``ai_input.json`` schema.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .transcribe import (
    DEFAULT_CHUNK_MINUTES,
    DEFAULT_MODEL_ID,
    Segment,
    Transcriber,
    format_timestamp_plain,
    write_transcript,
)
from .vocabulary import load as load_vocabulary
from .vocabulary import resolve_vocab_path, to_context_string

_METADATA_FILE = "metadata.json"
_AI_INPUT_FILE = "ai_input.json"
_TRANSCRIPT_PATH = Path("transcript") / "transcript.md"
_AUDIO_PATH = Path("audio") / "recording.wav"
_SLIDES_DIRNAME = "slides"


def load_metadata(meeting_dir: str | Path) -> dict[str, Any]:
    """Read ``<meeting_dir>/metadata.json`` as a dict.

    Raises FileNotFoundError if either the meeting directory or its
    metadata.json is missing — both are upstream contracts from the
    `record` subcommand and a missing one indicates a wrong path.
    """
    meeting_dir = Path(meeting_dir)
    if not meeting_dir.is_dir():
        raise FileNotFoundError(
            f"Meeting directory not found: {meeting_dir}"
            f" / 找不到会议目录:{meeting_dir}"
        )
    meta_path = meeting_dir / _METADATA_FILE
    if not meta_path.is_file():
        raise FileNotFoundError(
            f"metadata.json not found at {meta_path}"
            f" / 找不到元数据文件:{meta_path}"
        )
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _format_signed_offset(seconds: int) -> str:
    """Render a signed second-offset as ``[+-]HH:MM:SS`` (sign omitted for 0)."""
    if seconds == 0:
        return format_timestamp_plain(0)
    sign = "-" if seconds < 0 else "+"
    return f"{sign}{format_timestamp_plain(abs(seconds))}"


def _slide_offset_seconds(slide_timestamp: str, start_time: str) -> int:
    """Wall-clock difference in seconds. Negative = slide before record start.

    Assumes both timestamps fall on the same calendar day; cross-midnight
    recordings are out of scope (no sane meeting wraps midnight).
    """
    fmt = "%H:%M:%S"
    slide_dt = datetime.strptime(slide_timestamp, fmt)
    start_dt = datetime.strptime(start_time, fmt)
    return int((slide_dt - start_dt).total_seconds())


def build_ai_input(
    metadata: dict[str, Any], segments: list[Segment]
) -> dict[str, Any]:
    """Pure transform: metadata + segments → the ai_input.json payload.

    Schema (stable contract with P3.2 AI orchestration):

      {
        "meta": { topic, date, start_time, duration_seconds,
                  audio_duration_seconds, process_keyword, window_keyword },
        "slides": [
          { index, type, slide_number, path (relative, forward slashes),
            captured_at (wall clock), offset_seconds, offset (signed string) }
        ],
        "transcript_segments": [
          { index, start_seconds, end_seconds, start, end, text }
        ]
      }

    Path normalization rationale: metadata.json stores Windows absolute
    paths (e.g. ``meetings\\<topic>\\slides\\slide_001.png``). For an AI
    consumer running in any context — Claude Code, a future skill,
    cross-machine tooling — we emit forward-slash paths relative to the
    meeting directory so the consumer can resolve them against any base.
    """
    start_time = metadata.get("start_time", "00:00:00")

    slides_out: list[dict[str, Any]] = []
    for idx, slide in enumerate(metadata.get("slides", []), start=1):
        filename = slide.get("filename", "")
        slide_ts = slide.get("timestamp", start_time)
        try:
            offset = _slide_offset_seconds(slide_ts, start_time)
        except ValueError:
            # Malformed timestamp in metadata — emit 0 rather than crash.
            offset = 0
        slides_out.append({
            "index": idx,
            "type": slide.get("type", "slide"),
            "slide_number": slide.get("slide_number", idx),
            "path": f"{_SLIDES_DIRNAME}/{filename}" if filename else "",
            "captured_at": slide_ts,
            "offset_seconds": offset,
            "offset": _format_signed_offset(offset),
        })

    transcript_out: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments, start=1):
        transcript_out.append({
            "index": idx,
            "start_seconds": seg.start,
            "end_seconds": seg.end,
            "start": format_timestamp_plain(seg.start),
            "end": format_timestamp_plain(seg.end),
            "text": seg.text,
        })

    return {
        "meta": {
            "topic": metadata.get("topic"),
            "date": metadata.get("date"),
            "start_time": start_time,
            "duration_seconds": metadata.get("duration_seconds"),
            "audio_duration_seconds": metadata.get("audio_duration_seconds"),
            "process_keyword": metadata.get("process_keyword"),
            "window_keyword": metadata.get("window_keyword"),
        },
        "slides": slides_out,
        "transcript_segments": transcript_out,
    }


def run(
    meeting_dir: str | Path,
    *,
    model_id: str = DEFAULT_MODEL_ID,
    device: str = "auto",
    vocab_path: str | Path | None = None,
    chunk_minutes: float = DEFAULT_CHUNK_MINUTES,
    force: bool = False,
) -> Path:
    """Orchestrate transcribe + ai_input emission for one meeting.

    Returns the path to ``ai_input.json``. See module docstring for the
    idempotency contract and write ordering.
    """
    meeting_dir = Path(meeting_dir)
    metadata = load_metadata(meeting_dir)

    audio_path = meeting_dir / _AUDIO_PATH
    transcript_path = meeting_dir / _TRANSCRIPT_PATH
    ai_input_path = meeting_dir / _AI_INPUT_FILE

    # Idempotency: transcript.md is the "done" marker (written last on
    # success). If it's there and the user didn't ask for --force, we
    # assume the prior run completed both files.
    if transcript_path.is_file() and not force:
        if not ai_input_path.is_file():
            raise RuntimeError(
                f"Transcript exists at {transcript_path} but ai_input.json is "
                f"missing — rerun with --force to rebuild."
                f" / 已有 transcript 但 ai_input.json 缺失，请用 --force 重做。"
            )
        return ai_input_path

    if not audio_path.is_file():
        raise FileNotFoundError(
            f"Audio file not found: {audio_path}"
            f" / 找不到音频文件:{audio_path}"
        )

    resolved_vocab = resolve_vocab_path(vocab_path, meeting_dir)
    vocab_context = ""
    if resolved_vocab is not None:
        parsed = load_vocabulary(resolved_vocab)
        vocab_context = to_context_string(parsed)

    transcriber = Transcriber(model_id=model_id, device=device)
    segments = transcriber.transcribe(
        audio_path,
        vocab_context=vocab_context,
        chunk_minutes=chunk_minutes,
    )

    ai_input = build_ai_input(metadata, segments)

    # Write ai_input.json first, transcript.md last — see module docstring
    # for why the order matters (crash-safe idempotency marker).
    ai_input_path.write_text(
        json.dumps(ai_input, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_transcript(segments, transcript_path)

    return ai_input_path
