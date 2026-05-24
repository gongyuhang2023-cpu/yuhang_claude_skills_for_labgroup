"""Command-line entry point for MeetingMind."""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from . import __version__, postprocess
from .audio import AudioRecorder
from .process_finder import ProcessInfo, find_audio_sessions, find_by_keyword
from .session import MeetingSession
from .slides import SlideCapture
from .transcribe import (
    DEFAULT_CHUNK_MINUTES,
    DEFAULT_MODEL_ID,
    Transcriber,
    write_transcript,
)
from .vocabulary import load as load_vocabulary
from .vocabulary import resolve_vocab_path, to_context_string

EPILOG = """\
status:
  Phase 1 available: list-processes, record.
  Phase 2 available: transcribe, postprocess.
  Internal dev helpers: _record-audio, _capture-slides (will be removed
  once the full record flow stabilizes).

子命令开发中 — 详见 docs/PLAN.md。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meetingmind",
        description=(
            "Auto-record online meetings (Teams/Zoom/Tencent): audio + PPT "
            "screenshots + transcription + AI summary. "
            "Volume-independent capture via Windows Process Loopback."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"meetingmind {__version__}",
    )

    subparsers = parser.add_subparsers(
        title="commands",
        dest="command",
        metavar="<command>",
    )

    p_list = subparsers.add_parser(
        "list-processes",
        help="List processes that may be producing audio",
        description=(
            "List processes that are either currently producing audio "
            "(WASAPI session active) or are known media/meeting apps. "
            "Use --filter to search by app name or preset keyword."
        ),
    )
    p_list.add_argument(
        "--filter",
        "-f",
        dest="filter_keyword",
        default=None,
        metavar="KEYWORD",
        help=(
            "Filter by keyword: preset (teams|zoom|tencent|edge|chrome|firefox) "
            "or substring match against executable name (case-insensitive)."
        ),
    )

    # Internal-use subcommand: isolated audio-only recording for P1.3.
    # The user-facing `record` command (P1.5) will subsume this.
    p_rec = subparsers.add_parser(
        "_record-audio",
        help="(internal) Record audio only from a specific PID for testing",
        description=(
            "Record audio from a specific PID via ProcTap and write a WAV. "
            "Intended for development verification of the audio module; "
            "the public `record` subcommand (Phase 1.5) will replace this."
        ),
    )
    p_rec.add_argument(
        "--pid",
        type=int,
        required=True,
        help="Target process PID (find via `list-processes`)",
    )
    p_rec.add_argument(
        "--duration",
        "-d",
        type=float,
        default=None,
        help="Recording duration in seconds; omit to record until Ctrl+C.",
    )
    p_rec.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output WAV path (default: meetings/_audio-test-YYYYMMDD-HHMMSS.wav)",
    )

    # Internal-use subcommand: isolated slide-capture for P1.4 verification.
    p_slide = subparsers.add_parser(
        "_capture-slides",
        help="(internal) Capture slide screenshots from a window for testing",
        description=(
            "Capture PPT screenshots from a window via WGC and write PNG "
            "files under --output-dir. Intended for development verification; "
            "the public `record` subcommand (Phase 1.5) will replace this."
        ),
    )
    p_slide.add_argument(
        "--window",
        type=str,
        required=True,
        help="Window title substring or preset key (teams|zoom|tencent)",
    )
    p_slide.add_argument(
        "--duration",
        "-d",
        type=float,
        default=None,
        help="Capture duration in seconds; omit to capture until Ctrl+C.",
    )
    p_slide.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds (default: 5).",
    )
    p_slide.add_argument(
        "--threshold",
        type=float,
        default=5.0,
        help="Change-ratio threshold to trigger a new slide capture (default: 5%%).",
    )
    p_slide.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Slide output directory (default: meetings/_slides-test-YYYYMMDD-HHMMSS/)",
    )

    # User-facing recording command (subsumes _record-audio + _capture-slides).
    p_record = subparsers.add_parser(
        "record",
        help="Record a meeting (audio + slide screenshots)",
        description=(
            "Record an online meeting end-to-end: process-loopback audio "
            "(volume-independent) plus WGC slide screenshots, written to "
            "meetings/YYYY-MM-DD-{topic}/. Stop with Ctrl+C or by creating "
            "a STOP file in the meeting directory."
        ),
    )
    p_record.add_argument(
        "--process",
        type=str,
        required=True,
        help="Process keyword: preset (teams|zoom|tencent) or substring",
    )
    p_record.add_argument(
        "--topic",
        type=str,
        default=None,
        help="Short slug for the meeting directory (e.g. 'lab-meeting')",
    )
    p_record.add_argument(
        "--output-root",
        type=Path,
        default=Path("meetings"),
        help="Root directory for meeting folders (default: ./meetings)",
    )
    p_record.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Slide capture polling interval in seconds (default: 5)",
    )
    p_record.add_argument(
        "--threshold",
        type=float,
        default=5.0,
        help="Slide change-ratio threshold to trigger a new capture (default: 5%%)",
    )
    p_record.add_argument(
        "--pid",
        type=int,
        default=None,
        help="Advanced: pin to a specific PID, skipping auto-detection",
    )
    p_record.add_argument(
        "--window",
        type=str,
        default=None,
        help="Advanced: override the window title keyword used for slide capture",
    )
    p_record.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress progress prints. The meeting directory and STOP-file "
            "path are still printed on the first line of stdout for the caller."
        ),
    )

    p_transcribe = subparsers.add_parser(
        "transcribe",
        help="Transcribe a recorded meeting's audio to transcript.md",
        description=(
            "Run Qwen3-ASR over <meeting_dir>/audio/recording.wav and write "
            "<meeting_dir>/transcript/transcript.md with [HH:MM:SS] markers. "
            "First run downloads the ~3.4 GB model into the HuggingFace cache."
        ),
    )
    p_transcribe.add_argument(
        "meeting_dir",
        type=Path,
        help="Meeting directory produced by `record` (must contain audio/recording.wav)",
    )
    p_transcribe.add_argument(
        "--device",
        choices=("auto", "cuda", "cpu"),
        default="auto",
        help="Inference device (default: auto — cuda if available, else cpu)",
    )
    p_transcribe.add_argument(
        "--vocab",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Hot-words vocabulary file. If omitted, probes "
            "<meeting_dir>/vocabulary.txt then ./vocabulary.txt. "
            "Missing-but-explicit path is a hard error."
        ),
    )
    p_transcribe.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL_ID,
        metavar="ID",
        help=f"HuggingFace model id (default: {DEFAULT_MODEL_ID})",
    )
    p_transcribe.add_argument(
        "--chunk-minutes",
        type=float,
        default=DEFAULT_CHUNK_MINUTES,
        metavar="N",
        help=(
            f"Split long audio into N-minute chunks for ASR "
            f"(default: {DEFAULT_CHUNK_MINUTES}). Each chunk yields one "
            "[HH:MM:SS] marker in the transcript."
        ),
    )
    p_transcribe.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress CLI summary lines (vocabulary banner, final segment count). "
            "Chunk-by-chunk transcriber progress on stderr is always emitted so "
            "long meetings still show liveness."
        ),
    )

    p_postprocess = subparsers.add_parser(
        "postprocess",
        help="Assemble transcript + ai_input.json for downstream AI summary",
        description=(
            "Run transcribe (if not already done) and emit <meeting_dir>/ai_input.json "
            "— a Claude-readable file combining metadata summary, slide index, and "
            "transcript segments. Idempotent: presence of "
            "transcript/transcript.md skips the ASR step unless --force is given."
        ),
    )
    p_postprocess.add_argument(
        "meeting_dir",
        type=Path,
        help="Meeting directory produced by `record` (must contain metadata.json)",
    )
    p_postprocess.add_argument(
        "--device",
        choices=("auto", "cuda", "cpu"),
        default="auto",
        help="Inference device for the transcribe step (default: auto)",
    )
    p_postprocess.add_argument(
        "--vocab",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Hot-words vocabulary file. If omitted, probes "
            "<meeting_dir>/vocabulary.txt then ./vocabulary.txt. "
            "Missing-but-explicit path is a hard error."
        ),
    )
    p_postprocess.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL_ID,
        metavar="ID",
        help=f"HuggingFace model id (default: {DEFAULT_MODEL_ID})",
    )
    p_postprocess.add_argument(
        "--chunk-minutes",
        type=float,
        default=DEFAULT_CHUNK_MINUTES,
        metavar="N",
        help=f"Per-chunk minutes for ASR (default: {DEFAULT_CHUNK_MINUTES}).",
    )
    p_postprocess.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-run even if transcript.md is already present. Overwrites both "
            "transcript.md and ai_input.json."
        ),
    )
    p_postprocess.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress CLI summary lines. As with `transcribe`, chunk-by-chunk "
            "transcriber progress on stderr is always emitted."
        ),
    )

    return parser


def _format_table(rows: list[ProcessInfo]) -> str:
    """Render a fixed-width ASCII table. Returns "" for empty input."""
    if not rows:
        return ""

    header_pid = "PID"
    header_name = "NAME"
    header_audio = "AUDIO"

    name_width = max(len(header_name), max(len(r["name"]) for r in rows))
    name_width = min(name_width, 40)  # cap absurdly long names

    pid_col = 8
    audio_col = 5
    fmt = f"{{pid:>{pid_col}}}  {{name:<{name_width}}}  {{audio:^{audio_col}}}"

    sep_line = "-" * pid_col + "  " + "-" * name_width + "  " + "-" * audio_col

    lines = [
        fmt.format(pid=header_pid, name=header_name, audio=header_audio),
        sep_line,
    ]
    for r in rows:
        name = r["name"]
        if len(name) > name_width:
            name = name[: name_width - 1] + "…"
        audio = "ON" if r["has_active_session"] else "-"
        lines.append(fmt.format(pid=r["pid"], name=name, audio=audio))

    active_count = sum(1 for r in rows if r["has_active_session"])
    lines.append("")
    lines.append(f"{len(rows)} processes ({active_count} active)")
    return "\n".join(lines)


def _run_list_processes(args: argparse.Namespace) -> int:
    rows = (
        find_by_keyword(args.filter_keyword)
        if args.filter_keyword
        else find_audio_sessions()
    )

    if not rows:
        kw = args.filter_keyword
        if kw:
            print(f"No processes match '{kw}'.", file=sys.stderr)
            print("  无匹配进程 / no matching process.", file=sys.stderr)
            print(
                "Hint: try `python -m meetingmind list-processes` "
                "(no filter) to see all audio-capable processes.",
                file=sys.stderr,
            )
        else:
            print("No audio-capable processes detected.", file=sys.stderr)
            print("  未检测到音频相关进程。", file=sys.stderr)
            print(
                "Hint: open a media app (Edge/Teams/Spotify) and try again.",
                file=sys.stderr,
            )
        return 1

    print(_format_table(rows))
    return 0


def _default_audio_test_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("meetings") / f"_audio-test-{stamp}.wav"


def _run_record_audio(args: argparse.Namespace) -> int:
    output: Path = args.output if args.output else _default_audio_test_path()
    duration: float | None = args.duration

    print(f"[record] PID         : {args.pid}", file=sys.stderr)
    print(f"[record] Output      : {output}", file=sys.stderr)
    print(
        f"[record] Duration    : {duration}s" if duration else
        "[record] Duration    : until Ctrl+C",
        file=sys.stderr,
    )
    print("[record] Format      : 48000 Hz / 2ch / int16 WAV", file=sys.stderr)
    print("[record] Press Ctrl+C to stop.", file=sys.stderr)

    rec = AudioRecorder(pid=args.pid, output_path=output)
    interrupted = False
    try:
        rec.start()
    except Exception as exc:  # noqa: BLE001 — surface ProcTap errors to user
        print(f"[record] Failed to start: {exc!r}", file=sys.stderr)
        print(f"  录制启动失败 / failed to start recording: {exc}", file=sys.stderr)
        return 2

    start_ts = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - start_ts
            if duration is not None and elapsed >= duration:
                break
            # Short sleep so KeyboardInterrupt is responsive
            time.sleep(0.2)
    except KeyboardInterrupt:
        interrupted = True
        print("\n[record] Ctrl+C received, finalizing...", file=sys.stderr)
    finally:
        try:
            rec.stop()
        except Exception as exc:  # noqa: BLE001 — defense-in-depth; AudioRecorder.stop is itself resilient
            print(f"[record] stop() raised unexpectedly: {exc!r}", file=sys.stderr)

    duration_actual = rec.duration_seconds
    size_mb = output.stat().st_size / (1024 * 1024) if output.exists() else 0.0
    print(f"[record] Captured    : {duration_actual:.1f}s ({size_mb:.1f} MB)", file=sys.stderr)
    print(f"[record] File        : {output}", file=sys.stderr)
    if rec.stop_errors:
        print("[record] Non-fatal teardown errors:", file=sys.stderr)
        for err in rec.stop_errors:
            print(f"  - {err}", file=sys.stderr)

    return 130 if interrupted else 0


def _default_slides_test_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("meetings") / f"_slides-test-{stamp}"


def _resolve_window_keyword(raw: str) -> str:
    """Map a preset (teams/zoom/tencent) to its title substring; else passthrough."""
    from .slides import MEETING_PRESETS
    return MEETING_PRESETS.get(raw.lower(), raw)


def _run_capture_slides(args: argparse.Namespace) -> int:
    output_dir: Path = args.output_dir if args.output_dir else _default_slides_test_dir()
    title = _resolve_window_keyword(args.window)

    print(f"[slides] Window      : {title}", file=sys.stderr)
    print(f"[slides] Output      : {output_dir}", file=sys.stderr)
    print(
        f"[slides] Duration    : {args.duration}s" if args.duration else
        "[slides] Duration    : until Ctrl+C",
        file=sys.stderr,
    )
    print(
        f"[slides] Interval/Th : {args.interval}s / {args.threshold}%",
        file=sys.stderr,
    )
    print("[slides] Press Ctrl+C to stop.", file=sys.stderr)

    cap = SlideCapture(
        output_dir=output_dir,
        title_keyword=title,
        interval=args.interval,
        threshold=args.threshold,
    )
    interrupted = False
    try:
        cap.start()
    except Exception as exc:  # noqa: BLE001 — surface WGC/window errors to user
        print(f"[slides] Failed to start: {exc!r}", file=sys.stderr)
        print(f"  捕获启动失败 / failed to start capture: {exc}", file=sys.stderr)
        return 2

    start_ts = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - start_ts
            if args.duration is not None and elapsed >= args.duration:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        interrupted = True
        print("\n[slides] Ctrl+C received, finalizing...", file=sys.stderr)

    meta = cap.stop()
    slide_count = sum(1 for m in meta if m.get("type") == "slide")
    revisit_count = sum(1 for m in meta if m.get("type") == "revisit")
    print(
        f"[slides] Captured    : {slide_count} unique slides, {revisit_count} revisits",
        file=sys.stderr,
    )
    print(f"[slides] Directory   : {output_dir}", file=sys.stderr)

    return 130 if interrupted else 0


def _run_record(args: argparse.Namespace) -> int:
    sess = MeetingSession(
        process_keyword=args.process,
        topic=args.topic,
        output_root=args.output_root,
        interval=args.interval,
        threshold=args.threshold,
        pid=args.pid,
        window_keyword=args.window,
    )

    try:
        sess.start()
    except Exception as exc:  # noqa: BLE001 — surface friendly errors to user
        print(f"[record] Failed to start: {exc}", file=sys.stderr)
        return 2

    # Always print the meeting dir + STOP file path on stdout's first line so
    # a backgrounding caller (the skill harness) can capture them without
    # needing to scrape any other output.
    print(str(sess.meeting_dir))
    print(str(sess.stop_file))
    sys.stdout.flush()

    if not args.quiet:
        print(f"[record] Meeting dir : {sess.meeting_dir}", file=sys.stderr)
        print(f"[record] STOP file   : {sess.stop_file}", file=sys.stderr)
        print(f"[record] Interval/Th : {args.interval}s / {args.threshold}%", file=sys.stderr)
        print(
            "[record] To stop     : Ctrl+C, or `touch` the STOP file path above.",
            file=sys.stderr,
        )

    interrupted = False
    try:
        sess.wait()
    except KeyboardInterrupt:
        interrupted = True
        if not args.quiet:
            print("\n[record] Ctrl+C received, finalizing...", file=sys.stderr)

    summary = sess.stop()
    if not args.quiet:
        print(
            f"[record] Done. {summary['duration_seconds']}s, "
            f"{summary['slides_count']} slides "
            f"({summary['revisits_count']} revisits)",
            file=sys.stderr,
        )
        print(f"[record] Output      : {summary['meeting_dir']}", file=sys.stderr)

    return 130 if interrupted else 0


def _run_transcribe(args: argparse.Namespace) -> int:
    meeting_dir: Path = args.meeting_dir.resolve()
    if not meeting_dir.is_dir():
        print(
            f"Error: meeting directory not found: {meeting_dir}"
            f" / 找不到会议目录：{meeting_dir}",
            file=sys.stderr,
        )
        return 1

    audio_path = meeting_dir / "audio" / "recording.wav"
    if not audio_path.is_file():
        print(
            f"Error: audio not found at {audio_path}"
            f" / 找不到音频文件：{audio_path}",
            file=sys.stderr,
        )
        return 1

    try:
        vocab_path = resolve_vocab_path(args.vocab, meeting_dir)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    vocab_context = ""
    if vocab_path is not None:
        parsed = load_vocabulary(vocab_path)
        vocab_context = to_context_string(parsed)
        if not args.quiet:
            print(
                f"[transcribe] vocabulary: {vocab_path} "
                f"({sum(len(v) for v in parsed.values())} terms)",
                file=sys.stderr,
            )
    elif not args.quiet:
        print(
            "[transcribe] no vocabulary file found — continuing without hot-words. "
            "/ 未找到热词文件，继续转录但不使用领域热词。",
            file=sys.stderr,
        )

    try:
        transcriber = Transcriber(model_id=args.model, device=args.device)
        segments = transcriber.transcribe(
            audio_path,
            vocab_context=vocab_context,
            chunk_minutes=args.chunk_minutes,
        )
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    transcript_path = meeting_dir / "transcript" / "transcript.md"
    write_transcript(segments, transcript_path)

    # stdout = the artifact path, so shell pipelines (and the future skill
    # wrapper) can pick it up without parsing progress logs.
    print(str(transcript_path))
    if not args.quiet:
        print(
            f"[transcribe] wrote {len(segments)} segment(s) to {transcript_path}",
            file=sys.stderr,
        )
    return 0


def _run_postprocess(args: argparse.Namespace) -> int:
    meeting_dir: Path = args.meeting_dir.resolve()
    try:
        ai_input_path = postprocess.run(
            meeting_dir,
            model_id=args.model,
            device=args.device,
            vocab_path=args.vocab,
            chunk_minutes=args.chunk_minutes,
            force=args.force,
        )
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # stdout = the ai_input.json path so shell pipelines / the future skill
    # wrapper can pick it up without parsing stderr noise.
    print(str(ai_input_path))
    if not args.quiet:
        transcript_path = meeting_dir / "transcript" / "transcript.md"
        print(
            f"[postprocess] ai_input    : {ai_input_path}",
            file=sys.stderr,
        )
        print(
            f"[postprocess] transcript  : {transcript_path}",
            file=sys.stderr,
        )
    return 0


_DISPATCH = {
    "list-processes": _run_list_processes,
    "_record-audio": _run_record_audio,
    "_capture-slides": _run_capture_slides,
    "record": _run_record,
    "transcribe": _run_transcribe,
    "postprocess": _run_postprocess,
}


def _harden_stdio_encoding() -> None:
    """Force UTF-8 on stdout/stderr so non-ASCII window titles and bilingual
    error messages never crash the CLI on terminals defaulting to GBK/CP936.

    Characters the underlying terminal cannot render fall back to '?' instead
    of raising UnicodeEncodeError mid-pipeline.
    """
    for stream in (sys.stdout, sys.stderr):
        # Stream may be replaced by a wrapper (pytest capture, redirect to file
        # with explicit encoding, etc.) — silently leave it alone in that case.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(OSError):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _harden_stdio_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    handler = _DISPATCH.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2  # unreachable; parser.error exits
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
