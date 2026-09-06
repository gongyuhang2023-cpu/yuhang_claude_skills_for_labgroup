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
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio import AudioRecorder, SystemAudioRecorder
from .mic import CHANNELS as _MIC_CHANNELS
from .mic import MicRecorder
from .process_finder import find_by_keyword, root_audio_pid
from .slides import MEETING_PRESETS, SlideCapture, SlideMetadata

# Stable on-disk layout for a single meeting.
_AUDIO_FILE = "audio/recording.wav"
_MIC_FILE = "audio/mic.wav"
_MIC_SAMPLE_RATE_FALLBACK = 44100   # 只在设备没报采样率时兜底
_SLIDES_DIR = "slides"
_METADATA_FILE = "metadata.json"
_STOP_FILE = "STOP"

# Slug character class: keep CJK + ASCII letters/digits, strip everything else.
_SLUG_KEEP = re.compile(r"[^\w一-鿿-]+", re.UNICODE)


def write_json_atomic(target: Path, data: dict[str, Any]) -> None:
    """Write JSON so readers only ever see the old file or the new one.

    A plain write truncates first, so being killed mid-write replaces a usable
    file with half a JSON object — the exact way a crash-recovery mechanism
    would end up destroying the thing it exists to protect. `os.replace` is
    atomic on Windows and POSIX alike.

    Shared with `recovery.py`: two independently written versions of this would
    drift, and the subtle one would be wrong.
    """
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Windows refuses to replace a file somebody has open (an editor, a sync
    # client). Give it a moment rather than losing the update.
    last_error: OSError | None = None
    for attempt in range(3):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1 * (attempt + 1))
    with contextlib.suppress(OSError):
        tmp.unlink()
    raise RuntimeError(
        f"could not update {target} (is it open elsewhere?): {last_error!r}"
    )


def _warn(message: str) -> None:
    """Emit a diagnostic that can never abort the caller.

    Two hard-won details:

    * **stderr, not stdout.** stdout carries the meeting_dir and STOP paths the
      skill harness parses; diagnostics do not belong in that channel.
    * **Wrapped.** These messages contain non-ASCII (⚠, Chinese). On a console
      whose encoding cannot represent it — a GBK terminal, the Windows default
      here — `print` raises UnicodeEncodeError. That used to propagate out of
      `stop()` and skip the metadata write that came next: a silent recording
      turned into a recording with no metadata at all. Observed, not theorised.
    """
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — a warning must not become a failure
        with contextlib.suppress(Exception):
            print(
                message.encode("ascii", "replace").decode("ascii"),
                file=sys.stderr,
                flush=True,
            )

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
        process_keyword: str | None,
        topic: str | None,
        output_root: Path,
        interval: float = 5.0,
        threshold: float = 5.0,
        mic: bool = False,
        pid: int | None = None,
        window_keyword: str | None = None,
        monitor_index: int | None = None,
        capture_slides: bool = True,
        system_audio: bool = False,
        mic_only: bool = False,
    ) -> None:
        self._process_keyword = process_keyword
        self._topic = topic
        self._output_root = Path(output_root)
        self._interval = float(interval)
        self._threshold = float(threshold)
        self._mic = bool(mic)
        self._pid_override = pid
        self._window_override = window_keyword
        self._monitor_index = monitor_index
        self._capture_slides = bool(capture_slides)
        self._system_audio = bool(system_audio)
        self._mic_only = bool(mic_only)

        self._stop_event = threading.Event()
        self._started = False
        self._stopped = False

        self._meeting_dir: Path | None = None
        self._start_time: datetime | None = None
        self._end_time: datetime | None = None
        self._resolved_pid: int | None = None
        self._attributed_pid: int | None = None  # pre-rooting pid (diagnostics)
        self._resolved_window: str | None = None

        self._recorder: AudioRecorder | SystemAudioRecorder | None = None
        self._capture: SlideCapture | None = None
        self._slides_meta: list[SlideMetadata] = []
        self._final_audio_duration: float | None = None
        self._audio_device_label: str | None = None
        self._mic_recorder: MicRecorder | None = None
        self._final_mic_duration: float | None = None
        # Writer/gate counters snapshotted at stop(). These are what say
        # whether anything was lost; metadata carries them so the answer is
        # still available long after the console output is gone.
        self._audio_stats: dict[str, Any] = {}
        self._audio_samplerate: int | None = None
        self._mic_stats: dict[str, Any] = {}

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
            attributed = int(self._pid_override)
        else:
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
            attributed = int(active[0]["pid"])

        # The audio session is usually attributed to a *leaf* helper process
        # whose subtree renders nothing (esp. new Teams → WebView2). Capture the
        # app's root process so proctap's tree-include grabs the real renderer.
        self._attributed_pid = attributed
        rooted = root_audio_pid(attributed)
        if rooted != attributed:
            print(
                f"[session] audio session on pid {attributed} → capturing app "
                f"root pid {rooted} (tree-include covers WebView2/child renderers)",
                file=sys.stderr,
                flush=True,
            )
        return rooted

    def _resolve_window_keyword(self) -> str | None:
        if self._window_override is not None:
            return self._window_override
        if self._process_keyword is None:      # mic-only / system-audio：没有进程可解
            return None
        return MEETING_PRESETS.get(self._process_keyword.lower(), self._process_keyword)

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._started:
            raise RuntimeError("MeetingSession already started")

        # 1. Resolve targets first — fail fast before touching the filesystem.
        #    System-audio mode captures the whole default output endpoint, so it
        #    needs no process/PID — and must NOT abort when no active session is
        #    found (independence from process attribution is the whole point of
        #    the fallback). Skip PID resolution entirely in that mode.
        #    Mic-only mode (`--mic-only`, 面对面开会) has no remote party at all —
        #    the room's single microphone is the whole recording. Like system
        #    audio, it needs no process/PID.
        self._resolved_pid = (
            None if (self._system_audio or self._mic_only) else self._resolve_pid()
        )
        self._resolved_window = self._resolve_window_keyword()

        # 2. Build directory layout.
        self._meeting_dir = _build_meeting_dir(self._output_root, self._topic)
        self._meeting_dir.mkdir(parents=True, exist_ok=False)
        (self._meeting_dir / "audio").mkdir(parents=True, exist_ok=True)
        if self._capture_slides:
            (self._meeting_dir / _SLIDES_DIR).mkdir(parents=True, exist_ok=True)

        # 3. Slides first (unless audio-only) — surfaces "window not found"
        #    errors immediately, while we still have nothing to roll back. Voice
        #    calls (/cafe-meeting) have no shareable window, so capture_slides=
        #    False records audio only and skips this entirely.
        if self._capture_slides:
            capture = SlideCapture(
                output_dir=self._meeting_dir / _SLIDES_DIR,
                title_keyword=self._resolved_window,
                interval=self._interval,
                threshold=self._threshold,
                on_window_lost=self._on_window_lost,
                monitor_index=self._monitor_index,
            )
            try:
                capture.start()
            except Exception:
                self._meeting_dir = None  # so a retry can reuse a fresh dir
                raise
            self._capture = capture

        # 4. Audio next. If it fails, undo the slide capture (if any).
        #    System-audio mode swaps the per-process recorder for a system-loopback
        #    one; the rest of the lifecycle is unchanged because both expose the
        #    same interface (has_signal / is_silent / duration_seconds / stop).
        audio_output = self._meeting_dir / _AUDIO_FILE
        recorder: AudioRecorder | SystemAudioRecorder | MicRecorder
        if self._mic_only:
            # **MicRecorder 占用主音频槽位，写 recording.wav。** 它的接口
            # （start/stop/duration_seconds/is_silent/sink_stats）与另外两个一致，
            # 所以 stop() 与 postprocess 都不用改 —— 面对面就是一条单路录音，
            # 只是源头从 loopback 换成了麦克风。
            recorder = MicRecorder(output_path=audio_output)
        elif self._system_audio:
            recorder = SystemAudioRecorder(output_path=audio_output)
        else:
            assert self._resolved_pid is not None  # guaranteed by _resolve_pid()
            recorder = AudioRecorder(pid=self._resolved_pid, output_path=audio_output)
        try:
            recorder.start()
        except Exception:
            # Roll back: stop the slide capture (if any) so WGC releases the window.
            if self._capture is not None:
                with contextlib.suppress(Exception):
                    self._capture.stop()
                self._capture = None
            raise
        self._recorder = recorder

        # 5. Optional second audio stream: the local microphone (the user's own
        #    voice), for two-stream 1:1 capture (`--mic`). A mic failure is
        #    non-fatal — keeping the loopback (the other side) beats aborting the
        #    whole recording over a mic glitch.
        if self._mic and not self._mic_only:
            mic_recorder = MicRecorder(output_path=self._meeting_dir / _MIC_FILE)
            try:
                mic_recorder.start()
                self._mic_recorder = mic_recorder
            except Exception as exc:  # noqa: BLE001 — mic is supplementary
                print(
                    f"⚠️  [session] 麦克风录制启动失败,仅录对方声音继续 / mic capture "
                    f"failed to start, continuing with loopback only: {exc}",
                    file=sys.stderr, flush=True,
                )
                self._mic_recorder = None

        self._start_time = datetime.now()
        self._started = True

        # Publish metadata straight away, marked as in progress. If this
        # recording never reaches stop(), what is on disk is still a meeting
        # directory that `recover` can finish rather than an orphaned WAV.
        self._write_provisional_metadata()

    def request_stop(self) -> None:
        """Ask wait() to return on the next poll. Safe to call from any thread."""
        self._stop_event.set()

    def wait(self, poll_interval: float = 0.5, max_seconds: float | None = None) -> None:
        """Block until request_stop() is invoked or the STOP file appears.

        `max_seconds` adds a third exit: stop by itself after that long. Fixed-length
        recordings (voiceprint enrollment — "talk for 60 seconds") otherwise need a
        second manual action at exactly the moment the user is least likely to
        remember it. Leave it None and the behaviour is unchanged.
        """
        if not self._started:
            raise RuntimeError("MeetingSession.wait() requires start() first")
        stop_path = self.stop_file
        audio_checked = False
        wait_start = datetime.now()
        while not self._stop_event.is_set():
            if max_seconds is not None and \
                    (datetime.now() - wait_start).total_seconds() >= max_seconds:
                self._stop_event.set()
                break
            if stop_path.exists():
                self._stop_event.set()
                # Remove the marker so the directory can be archived cleanly.
                with contextlib.suppress(OSError):
                    stop_path.unlink()
                break
            self._stop_event.wait(timeout=poll_interval)
            if (
                not audio_checked
                and self._recorder is not None
                and (datetime.now() - wait_start).total_seconds() >= 10
            ):
                audio_checked = True
                if not self._recorder.has_signal:
                    _warn(
                        "\n⚠️  [session] 录制已 10 秒但未检测到任何音频信号！"
                        "音频捕获可能存在故障，录制结束后转录将为空。"
                        "请确认会议软件正在播放声音。\n"
                    )

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
                _warn(f"[session] SlideCapture.stop() raised: {exc!r}")
            self._capture = None

        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                _warn(f"[session] AudioRecorder.stop() raised: {exc!r}")
            # Snapshot the recorder's measured duration before dropping the ref;
            # _write_metadata reads it for `audio_duration_seconds`.
            self._final_audio_duration = self._recorder.duration_seconds
            self._audio_device_label = getattr(self._recorder, "device_label", None)
            # 采样率不能写死：mic-only 走麦克风的原生采样率（实测 44100），
            # 而 loopback 恒为 48000/2ch。写死会让 metadata 说谎
            self._audio_samplerate = getattr(self._recorder, "samplerate", None)
            self._audio_stats = dict(getattr(self._recorder, "sink_stats", {}) or {})
            if self._recorder.is_silent:
                _warn(
                    "\n⚠️  [session] Audio recording is COMPLETELY SILENT. "
                    "Transcript will be empty. Check audio capture setup.\n"
                )
            self._recorder = None

        if self._mic_recorder is not None:
            try:
                self._mic_recorder.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                _warn(f"[session] MicRecorder.stop() raised: {exc!r}")
            self._final_mic_duration = self._mic_recorder.duration_seconds
            self._mic_stats = dict(getattr(self._mic_recorder, "sink_stats", {}) or {})
            if self._mic_recorder.is_silent:
                _warn(
                    "\n⚠️  [session] 麦克风录音完全静音 —— 你的发言可能没录上。"
                    "请检查麦克风/输入设备。\n"
                )
            self._mic_recorder = None

        self._end_time = datetime.now()
        self._stopped = True

        self._report_capture_losses()

        summary = self._write_metadata()
        self._stop_summary = summary
        return summary

    # ----------------------------------------------------------------- internal

    def _on_window_lost(self) -> None:
        """SlideCapture's hook for when WGC reports the target window gone."""
        self.request_stop()

    def _build_metadata(self, status: str) -> dict[str, Any]:
        """Assemble metadata.json for `recording_status=status`.

        `start_time` is always a real ``%H:%M:%S`` string, never null, even in
        the provisional copy: `postprocess._slide_offset_seconds` feeds it to
        `strptime` and only catches ValueError, so a null there would raise
        TypeError and take the whole postprocess run down.
        """
        assert self._meeting_dir is not None
        assert self._start_time is not None

        slide_count = sum(1 for s in self._slides_meta if s.get("type") == "slide")
        revisit_count = sum(1 for s in self._slides_meta if s.get("type") == "revisit")
        # While recording there is no end yet; report a zero-length window
        # rather than inventing one.
        end_time = self._end_time or self._start_time
        duration = (end_time - self._start_time).total_seconds()

        return {
            "recording_status": status,
            "date": self._start_time.strftime("%Y-%m-%d"),
            "start_time": self._start_time.strftime("%H:%M:%S"),
            "end_time": end_time.strftime("%H:%M:%S"),
            "duration_seconds": int(duration),
            "topic": self._topic,
            "process_keyword": self._process_keyword,
            "window_keyword": self._resolved_window,
            "pid": self._resolved_pid,
            "pid_attributed": self._attributed_pid,
            "audio_mode": ("mic" if self._mic_only
                           else "system" if self._system_audio else "process"),
            "audio_device": self._audio_device_label,
            "audio_path": _AUDIO_FILE,
            "audio_sample_rate": (
                (self._audio_samplerate or _MIC_SAMPLE_RATE_FALLBACK)
                if self._mic_only else 48000
            ),
            "audio_channels": _MIC_CHANNELS if self._mic_only else 2,
            "audio_duration_seconds": self._final_audio_duration,
            "audio_stats": self._audio_stats,
            "mic_path": _MIC_FILE if self._mic else None,
            "mic_captured": self._final_mic_duration is not None,
            "mic_duration_seconds": self._final_mic_duration,
            "mic_stats": self._mic_stats,
            "slides_count": slide_count,
            "revisits_count": revisit_count,
            "slides": self._slides_meta,
            "parameters": {
                "interval": self._interval,
                "threshold": self._threshold,
                "mic": self._mic,
                "system_audio": self._system_audio,
                "mic_only": self._mic_only,
            },
        }

    def _write_metadata_file(self, meta: dict[str, Any]) -> None:
        assert self._meeting_dir is not None
        write_json_atomic(self._meeting_dir / _METADATA_FILE, meta)

    def _write_provisional_metadata(self) -> None:
        """Publish metadata.json the moment recording starts.

        Without this, a recording that dies before `stop()` leaves audio and
        screenshots on disk that `postprocess` refuses to touch, because it
        requires metadata.json and there is none. Best-effort: failing to write
        it must not prevent the recording itself.
        """
        try:
            self._write_metadata_file(self._build_metadata(status="recording"))
        except Exception as exc:  # noqa: BLE001 — never block the recording
            _warn(f"[session] could not write provisional metadata: {exc!r}")

    def _report_capture_losses(self) -> None:
        """Print a banner if any audio was lost, in a form that is hard to miss.

        Deliberately *not* an error exit code: a recording that lost a few
        seconds still needs transcribing, and a non-zero exit would make the
        skill harness treat the whole run as failed and skip that.
        """
        problems: list[str] = []
        for label, stats in (("audio", self._audio_stats), ("mic", self._mic_stats)):
            if not stats:
                continue
            if stats.get("writer_error"):
                problems.append(f"{label}: writer failed — {stats['writer_error']}")
            if stats.get("dropped_bytes"):
                problems.append(
                    f"{label}: {stats['dropped_bytes']} bytes dropped (disk too slow)"
                )
            if stats.get("late_bytes"):
                problems.append(
                    f"{label}: {stats['late_calls']} callback(s) arrived after stop"
                )
            if stats.get("size_limit_reached"):
                problems.append(f"{label}: hit the 4 GiB WAV limit (~6.2h)")
        if not problems:
            return
        _warn(
            "\n"
            + "=" * 68
            + "\n⚠️  [session] 录制过程中有音频丢失 / audio was lost during capture:\n  - "
            + "\n  - ".join(problems)
            + "\n"
            + "=" * 68
            + "\n"
        )

    def _recording_outcome(self) -> str:
        """`complete` only if every sink actually settled.

        A writer stuck in a blocked write makes `finish()` give up: the file is
        still being held, the duration and silence figures were never computed.
        Calling that `complete` would hand `postprocess` numbers nobody stands
        behind. `incomplete` sends it to `recover` instead, which recomputes
        them from the file once the dust has settled.
        """
        for stats in (self._audio_stats, self._mic_stats):
            if not stats:
                continue
            if stats.get("finish_timed_out") or not stats.get("finished"):
                return "incomplete"
        return "complete"

    def _write_metadata(self) -> dict[str, Any]:
        assert self._meeting_dir is not None
        assert self._end_time is not None

        outcome = self._recording_outcome()
        if outcome != "complete":
            _warn(
                "\n⚠️  [session] 音频收尾未完成（writer 仍被占用），"
                "metadata 标记为 incomplete。\n"
                "    audio never settled; run `recover` on this directory "
                "before postprocess.\n"
            )
        meta = self._build_metadata(status=outcome)
        self._write_metadata_file(meta)

        return {
            "meeting_dir": str(self._meeting_dir),
            "pid": self._resolved_pid,
            "duration_seconds": meta["duration_seconds"],
            "slides_count": meta["slides_count"],
            "revisits_count": meta["revisits_count"],
            "audio_path": str(self._meeting_dir / _AUDIO_FILE),
            "metadata_path": str(self._meeting_dir / _METADATA_FILE),
        }
