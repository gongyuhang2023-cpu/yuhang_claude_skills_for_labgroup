"""End-to-end meeting recording orchestration.

`MeetingSession` glues `AudioRecorder` (process-loopback audio) and
`SlideCapture` (WGC slide screenshots) into a single lifecycle:

  sess = MeetingSession(process_keyword="teams", topic="weekly", output_root=Path("meetings"))
  sess.start()
  sess.wait()        # blocks until STOP file appears or request_stop() is called
  summary = sess.stop()  # writes metadata.json, returns a dict for the caller

The class is intentionally side-effecting (it writes to disk) and not safe
for concurrent reuse. One instance per recording.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio import AudioRecorder
from .process_finder import find_by_keyword
from .slides import MEETING_PRESETS, SlideCapture, SlideMetadata

# Stable on-disk layout for a single meeting.
_AUDIO_FILE = "audio/recording.wav"
_SLIDES_DIR = "slides"
_METADATA_FILE = "metadata.json"
_STOP_FILE = "STOP"

# Slug character class: keep CJK + ASCII letters/digits, strip everything else.
_SLUG_KEEP = re.compile(r"[^\w一-鿿-]+", re.UNICODE)

# Windows-reserved basename device names. A meeting directory named "CON",
# "PRN", "AUX", "NUL", "COM1"–"COM9", or "LPT1"–"LPT9" is unusable on
# Windows filesystems even with a date prefix in some edge cases (e.g. if
# downstream tooling strips the prefix). Neutralize defensively.
_WIN_RESERVED: set[str] = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Cap slug length so the full meeting_dir path stays well under Windows
# MAX_PATH (260), leaving room for the date prefix, `-pN` collision suffix,
# and per-slide filenames like `slides/slide_NNN.png`.
_MAX_SLUG_LEN: int = 64


def _slugify(value: str | None) -> str:
    """Convert `value` to a filesystem-friendly slug.

    ASCII letters are lowercased; CJK characters are preserved as-is so users
    can keep meaningful Chinese topic names. Whitespace and punctuation collapse
    into single hyphens; leading/trailing hyphens are trimmed.
    """
    if value is None:
        return "meeting"
    text = value.strip()
    if not text:
        return "meeting"
    # Replace any run of non-keep characters with a single hyphen.
    slug = _SLUG_KEEP.sub("-", text)
    slug = re.sub(r"-+", "-", slug).strip("-")
    # Lowercase ASCII only — leave CJK alone.
    slug = "".join(c.lower() if c.isascii() else c for c in slug)
    if not slug:
        return "meeting"
    if slug.lower() in _WIN_RESERVED:
        slug = f"{slug}-meeting"
    if len(slug) > _MAX_SLUG_LEN:
        slug = slug[:_MAX_SLUG_LEN].rstrip("-") or "meeting"
    return slug


def _build_meeting_dir(output_root: Path, topic: str | None) -> Path:
    """Resolve a non-conflicting per-meeting directory under `output_root`.

    Returns `<output_root>/YYYY-MM-DD-{slug}` or `…-p{N}` if the base name
    already exists. Does not create the directory; caller is responsible.
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    slug = _slugify(topic)
    base_name = f"{date_str}-{slug}"
    candidate = output_root / base_name
    suffix = 2
    while candidate.exists():
        candidate = output_root / f"{base_name}-p{suffix}"
        suffix += 1
    return candidate


class MeetingSession:
    """Orchestrate concurrent audio + slide capture for one recording.

    Parameters mirror the eventual `record` CLI; advanced overrides (`pid`,
    `window_keyword`) skip auto-detection when the caller already knows what
    they want.
    """

    def __init__(
        self,
        process_keyword: str,
        topic: str | None,
        output_root: Path,
        interval: float = 5.0,
        threshold: float = 5.0,
        mic: bool = False,
        pid: int | None = None,
        window_keyword: str | None = None,
    ) -> None:
        self._process_keyword = process_keyword
        self._topic = topic
        self._output_root = Path(output_root)
        self._interval = float(interval)
        self._threshold = float(threshold)
        self._mic = bool(mic)
        self._pid_override = pid
        self._window_override = window_keyword

        self._stop_event = threading.Event()
        self._started = False
        self._stopped = False

        self._meeting_dir: Path | None = None
        self._start_time: datetime | None = None
        self._end_time: datetime | None = None
        self._resolved_pid: int | None = None
        self._resolved_window: str | None = None

        self._recorder: AudioRecorder | None = None
        self._capture: SlideCapture | None = None
        self._slides_meta: list[SlideMetadata] = []
        self._final_audio_duration: float | None = None

        self._stop_summary: dict[str, Any] | None = None

    # ----------------------------------------------------------------- props

    @property
    def meeting_dir(self) -> Path:
        if self._meeting_dir is None:
            raise RuntimeError("MeetingSession not started yet — meeting_dir is unset")
        return self._meeting_dir

    @property
    def stop_file(self) -> Path:
        return self.meeting_dir / _STOP_FILE

    # ----------------------------------------------------------------- resolve

    def _resolve_pid(self) -> int:
        if self._pid_override is not None:
            return int(self._pid_override)

        matches = find_by_keyword(self._process_keyword)
        if not matches:
            raise RuntimeError(
                f"No process matches '{self._process_keyword}'. "
                f"未找到匹配进程 / open the meeting app first."
            )

        active = [p for p in matches if p.get("has_active_session")]
        if not active:
            raise RuntimeError(
                f"No active audio session among {len(matches)} '{self._process_keyword}' "
                f"processes. 未检测到活跃音频会话 / join the meeting first."
            )
        return int(active[0]["pid"])

    def _resolve_window_keyword(self) -> str:
        if self._window_override is not None:
            return self._window_override
        return MEETING_PRESETS.get(self._process_keyword.lower(), self._process_keyword)

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._started:
            raise RuntimeError("MeetingSession already started")

        # 1. Resolve targets first — fail fast before touching the filesystem.
        self._resolved_pid = self._resolve_pid()
        self._resolved_window = self._resolve_window_keyword()

        # 2. Build directory layout.
        self._meeting_dir = _build_meeting_dir(self._output_root, self._topic)
        self._meeting_dir.mkdir(parents=True, exist_ok=False)
        (self._meeting_dir / "audio").mkdir(parents=True, exist_ok=True)
        (self._meeting_dir / _SLIDES_DIR).mkdir(parents=True, exist_ok=True)

        # 3. Slides first — surfaces "window not found" errors immediately,
        #    while we still have nothing to roll back.
        capture = SlideCapture(
            output_dir=self._meeting_dir / _SLIDES_DIR,
            title_keyword=self._resolved_window,
            interval=self._interval,
            threshold=self._threshold,
            on_window_lost=self._on_window_lost,
        )
        try:
            capture.start()
        except Exception:
            self._meeting_dir = None  # so a retry can reuse a fresh dir
            raise
        self._capture = capture

        # 4. Audio next. If it fails, undo the capture.
        recorder = AudioRecorder(
            pid=self._resolved_pid,
            output_path=self._meeting_dir / _AUDIO_FILE,
        )
        try:
            recorder.start()
        except Exception:
            # Roll back: stop the slide capture so WGC releases the window.
            with contextlib.suppress(Exception):
                capture.stop()
            self._capture = None
            raise
        self._recorder = recorder

        self._start_time = datetime.now()
        self._started = True

    def request_stop(self) -> None:
        """Ask wait() to return on the next poll. Safe to call from any thread."""
        self._stop_event.set()

    def wait(self, poll_interval: float = 0.5) -> None:
        """Block until request_stop() is invoked or the STOP file appears."""
        if not self._started:
            raise RuntimeError("MeetingSession.wait() requires start() first")
        stop_path = self.stop_file
        while not self._stop_event.is_set():
            if stop_path.exists():
                self._stop_event.set()
                # Remove the marker so the directory can be archived cleanly.
                with contextlib.suppress(OSError):
                    stop_path.unlink()
                break
            self._stop_event.wait(timeout=poll_interval)

    def stop(self) -> dict[str, Any]:
        """Stop slides → audio, write metadata.json, return a summary dict."""
        if not self._started:
            raise RuntimeError("MeetingSession not started — nothing to stop")
        if self._stopped:
            assert self._stop_summary is not None
            return self._stop_summary

        # Slides first so WGC releases the window before audio teardown.
        if self._capture is not None:
            try:
                self._slides_meta = self._capture.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                print(f"[session] SlideCapture.stop() raised: {exc!r}", flush=True)
            self._capture = None

        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                print(f"[session] AudioRecorder.stop() raised: {exc!r}", flush=True)
            # Snapshot the recorder's measured duration before dropping the ref;
            # _write_metadata reads it for `audio_duration_seconds`.
            self._final_audio_duration = self._recorder.duration_seconds
            self._recorder = None

        self._end_time = datetime.now()
        self._stopped = True

        summary = self._write_metadata()
        self._stop_summary = summary
        return summary

    # ----------------------------------------------------------------- internal

    def _on_window_lost(self) -> None:
        """SlideCapture's hook for when WGC reports the target window gone."""
        self.request_stop()

    def _write_metadata(self) -> dict[str, Any]:
        assert self._meeting_dir is not None
        assert self._start_time is not None
        assert self._end_time is not None

        slide_count = sum(1 for s in self._slides_meta if s.get("type") == "slide")
        revisit_count = sum(1 for s in self._slides_meta if s.get("type") == "revisit")
        duration = (self._end_time - self._start_time).total_seconds()

        audio_path_rel = _AUDIO_FILE  # canonical relative path
        audio_duration = self._final_audio_duration

        meta: dict[str, Any] = {
            "date": self._start_time.strftime("%Y-%m-%d"),
            "start_time": self._start_time.strftime("%H:%M:%S"),
            "end_time": self._end_time.strftime("%H:%M:%S"),
            "duration_seconds": int(duration),
            "topic": self._topic,
            "process_keyword": self._process_keyword,
            "window_keyword": self._resolved_window,
            "pid": self._resolved_pid,
            "audio_path": audio_path_rel,
            "audio_sample_rate": 48000,
            "audio_channels": 2,
            "audio_duration_seconds": audio_duration,
            "slides_count": slide_count,
            "revisits_count": revisit_count,
            "slides": self._slides_meta,
            "parameters": {
                "interval": self._interval,
                "threshold": self._threshold,
                "mic": self._mic,
            },
        }
        meta_path = self._meeting_dir / _METADATA_FILE
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        summary = {
            "meeting_dir": str(self._meeting_dir),
            "pid": self._resolved_pid,
            "duration_seconds": meta["duration_seconds"],
            "slides_count": slide_count,
            "revisits_count": revisit_count,
            "audio_path": str(self._meeting_dir / audio_path_rel),
            "metadata_path": str(meta_path),
        }
        return summary
