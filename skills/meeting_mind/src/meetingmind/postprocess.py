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
import sys
from dataclasses import replace
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
_MIC_PATH = Path("audio") / "mic.wav"
_SLIDES_DIRNAME = "slides"

# Speaker labels for two-stream 1:1 transcripts. The engine stays generic
# (loopback = the other party, mic = the user); the /cafe-meeting skill maps
# "对方" onto the actual advisor/senior in its digest.
_OTHER_LABEL = "对方"
_SELF_LABEL = "我"


def _warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


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


# Statuses that mean "this recording is finished and safe to process".
_FINISHED_STATUSES: frozenset[str] = frozenset({"complete", "recovered"})


def _require_finished_recording(
    meeting_dir: Path, metadata: dict[str, Any]
) -> None:
    """Refuse to process a recording that never finished.

    A session that dies before `stop()` leaves metadata marked ``recording``:
    no end time, zero duration, an empty slide index. Transcribing that would
    succeed and produce a permanently wrong ai_input.json — and because
    transcript.md doubles as the done-marker, the bad result would then be
    skipped over rather than rebuilt. Refusing is both cheaper and safer than
    guessing; `recover` exists to fill the gaps deliberately.

    The default is ``complete`` on purpose: every meeting recorded before this
    field existed has no ``recording_status`` at all, and treating those as
    unfinished would lock the entire back catalogue out of postprocessing.
    """
    status = metadata.get("recording_status", "complete")
    if status in _FINISHED_STATUSES:
        return
    raise RuntimeError(
        f"Recording did not finish cleanly (recording_status={status!r}). "
        f"Run this first:\n"
        f"    python -m meetingmind recover \"{meeting_dir}\"\n"
        f" / 录制未正常结束，请先运行上面的 recover 命令补全元数据。"
    )


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
            "speaker": seg.speaker,
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


def _plan_by_speaker(
    audio_path: Path,
    voiceprints: dict[str, str] | None,
    primary: str | None,
    other_name: str | None,
    num_speakers: int | None,
    device: str,
    max_block_minutes: float,
) -> tuple[list[tuple[float, float]], dict[float, str]] | None:
    """Work out who speaks when, and hand back cut points for the recogniser.

    Returns `(spans, speaker_by_start)`, or None when diarization is unavailable
    or finds nothing — in which case the caller transcribes normally and the
    transcript simply has no speaker labels. That degradation is deliberate: a
    meeting without labels is still a meeting, whereas no transcript is nothing.

    **The cut points have to come from here, not from a fixed chunk length.**
    Labelling after the fact cannot work: the recogniser would hand back one
    segment spanning several speakers, and a single label would then be stamped
    across all of them (measured: a two-minute alternating conversation came
    back as one segment credited entirely to one person).
    """
    from . import diarize as _diarize

    if not _diarize.available():
        _warn("[postprocess] pyannote 未安装，跳过说话人分离（转录不受影响）。")
        return None

    from .audioio import load_mono_16k, slice_waveform
    from .voiceprint import Embedder, Voiceprint, resolve_speakers

    dev = None if device == "auto" else device
    wav = load_mono_16k(audio_path)
    turns = _diarize.diarize(audio_path, num_speakers=num_speakers,
                             device=dev, waveform=wav)
    if not turns:
        _warn("[postprocess] 分离没有得到任何片段，转录保持不带说话人标注。")
        return None

    talk = _diarize.speaking_time(turns)
    print(f"[postprocess] diarize     : {len(turns)} turns / {len(talk)} speakers "
          + " · ".join(f"{k}={v:.0f}s" for k, v in talk.items()), file=sys.stderr)

    names: dict[str, str] = {}
    if voiceprints:
        embedder = Embedder(device=dev)
        prints = {n: Voiceprint.load(Path(path)) for n, path in voiceprints.items()}
        embeddings = {}
        for spk in _diarize.speakers(turns):
            chunks = [slice_waveform(wav, t.start, t.end)
                      for t in turns if t.speaker == spk]
            if chunks:
                import torch
                embeddings[spk] = embedder.embed(torch.cat(chunks, dim=1))
        for call in resolve_speakers(embeddings, prints, primary=primary,
                                     other_name=other_name):
            # No name only happens when there was nothing to name it with —
            # a third voice, or no `--other`. Then the diarizer's own label
            # stands, which at least keeps the speakers apart.
            names[call.label] = call.name or call.label
            print(f"[postprocess] speaker     : {call.label} → {names[call.label]} "
                  f"(confidence {call.confidence:+.2f})", file=sys.stderr)
    else:
        _warn("[postprocess] 没给声纹，说话人只能标成 SPEAKER_00/01。")

    blocks = _diarize.speaker_blocks(turns, max_seconds=max_block_minutes * 60.0)
    print(f"[postprocess] blocks      : {len(turns)} turns → {len(blocks)} "
          f"speaker blocks to transcribe", file=sys.stderr)
    spans = [(b.start, b.end) for b in blocks]
    speaker_by_start = {b.start: names.get(b.speaker, b.speaker) for b in blocks}
    return spans, speaker_by_start


def run(
    meeting_dir: str | Path,
    *,
    model_id: str = DEFAULT_MODEL_ID,
    device: str = "auto",
    vocab_path: str | Path | None = None,
    chunk_minutes: float = DEFAULT_CHUNK_MINUTES,
    force: bool = False,
    diarize: bool = False,
    voiceprints: dict[str, str] | None = None,
    primary: str | None = None,
    other_name: str | None = None,
    num_speakers: int | None = None,
) -> Path:
    """Orchestrate transcribe + ai_input emission for one meeting.

    Returns the path to ``ai_input.json``. See module docstring for the
    idempotency contract and write ordering.
    """
    meeting_dir = Path(meeting_dir)
    metadata = load_metadata(meeting_dir)
    _require_finished_recording(meeting_dir, metadata)

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

    # Diarization runs *before* transcription so it can decide where the cuts
    # go. Only for single-stream audio: with mic.wav present the speakers are
    # already separated by construction, and re-deriving them could only be worse.
    plan = None
    if diarize:
        if (meeting_dir / _MIC_PATH).is_file():
            _warn("[postprocess] 这是两路录音，说话人本来就是分开的 —— 跳过分离。")
        else:
            plan = _plan_by_speaker(
                audio_path, voiceprints, primary, other_name,
                num_speakers, device, chunk_minutes,
            )

    transcriber = Transcriber(model_id=model_id, device=device)
    segments = transcriber.transcribe(
        audio_path,
        vocab_context=vocab_context,
        chunk_minutes=chunk_minutes,
        partial_path=transcript_path.parent / "_partial.md",
        spans=(plan[0] if plan else None),
    )
    if plan:
        # Match on start time, not on position: spans that transcribe to nothing
        # (a cough, a 0.4 s "嗯") are dropped, so the two lists drift apart.
        speaker_by_start = plan[1]
        segments = [replace(s, speaker=speaker_by_start.get(s.start)) for s in segments]

    # Two-stream 1:1 recordings (`record --mic`) leave a second WAV with the
    # user's own voice. Transcribe it too and merge by timestamp, labelling each
    # stream by speaker — loopback = the other party, mic = the user. Group
    # recordings have no mic.wav and skip this, leaving behaviour byte-identical.
    mic_path = meeting_dir / _MIC_PATH
    if mic_path.is_file():
        segments = [replace(s, speaker=_OTHER_LABEL) for s in segments]
        mic_segments = transcriber.transcribe(
            mic_path,
            vocab_context=vocab_context,
            chunk_minutes=chunk_minutes,
            partial_path=transcript_path.parent / "_partial_mic.md",
        )
        mic_segments = [replace(s, speaker=_SELF_LABEL) for s in mic_segments]
        # Stable interleave by start time; at equal starts list the other party
        # first so a question→answer in one chunk reads in a natural order.
        segments = sorted(
            [*segments, *mic_segments],
            key=lambda s: (s.start, 0 if s.speaker == _OTHER_LABEL else 1),
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
