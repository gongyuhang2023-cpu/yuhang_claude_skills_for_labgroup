"""PPT slide capture from a Windows window via Windows Graphics Capture API.

Two layers:
  - Pure helpers (this file's top section): change detection, thumbnail
    fingerprinting, revisit deduplication. All testable without WGC.
  - SlideCapture class (below, in subsequent increments): WGC integration,
    polling loop, on-disk slide artefacts.

Algorithm sketch:
  1. WGC delivers a fresh BGRA frame whenever the target window repaints.
  2. Every `interval` seconds, compare the current frame against the previous
     "settled" frame at THUMB_SIZE thumbnail resolution.
  3. If the change exceeds `threshold` percent, debounce 0.5 s + 0.5 s and
     re-check. This rejects fly-through transitions and dropdowns.
  4. Once a frame settles, check it against every saved thumbnail. A match
     (change ratio < dedup_threshold) is logged as a revisit but not re-saved.
  5. New unique frames are written to disk as `slide_NNN.png`.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

import numpy as np
import pygetwindow as gw
import win32gui
from PIL import Image
from windows_capture import Frame, InternalCaptureControl, WindowsCapture

# Window-title substrings for common meeting apps. Used by callers to map a
# friendly keyword to the WGC target.
MEETING_PRESETS: dict[str, str] = {
    "teams": "Teams",
    "zoom": "Zoom Meeting",
    "tencent": "腾讯会议",
}

# Thumbnail resolution used for change detection. Smaller = faster, but at
# 640×480 we are still well above the resolution where slide text and figures
# remain distinguishable.
THUMB_SIZE: tuple[int, int] = (640, 480)


def _to_thumbnail(img: Image.Image) -> np.ndarray:
    """Resize to THUMB_SIZE RGB and return as uint8 ndarray (H, W, 3)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    resized = img.resize(THUMB_SIZE)
    return np.asarray(resized, dtype=np.uint8)


def _compare_thumbnails(a: np.ndarray, b: np.ndarray, pixel_threshold: int = 30) -> float:
    """Percentage of pixels whose mean-RGB difference exceeds `pixel_threshold`.

    Both arrays must be the same shape; the caller is responsible for resizing.
    """
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16)).mean(axis=2)
    changed = int((diff > pixel_threshold).sum())
    total = a.shape[0] * a.shape[1]
    return (changed / total) * 100.0


def compute_change_ratio(
    img_a: Image.Image, img_b: Image.Image, pixel_threshold: int = 30
) -> float:
    """Compare two PIL images at THUMB_SIZE.

    Returns the percentage of pixels whose mean RGB difference exceeds
    `pixel_threshold`. Inputs at different sizes are both downscaled to
    THUMB_SIZE before comparison.
    """
    thumb_a = _to_thumbnail(img_a)
    thumb_b = _to_thumbnail(img_b)
    return _compare_thumbnails(thumb_a, thumb_b, pixel_threshold)


def find_duplicate(
    img: Image.Image,
    saved_thumbnails: list[tuple[int, np.ndarray]],
    dedup_threshold: float = 3.0,
) -> int | None:
    """Return the slide_num whose saved thumbnail matches `img`, or None.

    A match means the change ratio is strictly less than `dedup_threshold`.
    The first matching slide_num is returned (saved_thumbnails is iterated
    in order — callers should preserve insertion order).
    """
    if not saved_thumbnails:
        return None
    thumb = _to_thumbnail(img)
    for saved_num, saved_thumb in saved_thumbnails:
        change = _compare_thumbnails(thumb, saved_thumb)
        if change < dedup_threshold:
            return saved_num
    return None


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------


def _set_dpi_awareness() -> None:
    """Make sure window coordinates and capture surfaces match physical pixels."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v1
    except OSError:
        with contextlib.suppress(OSError):
            ctypes.windll.user32.SetProcessDPIAware()


_set_dpi_awareness()


def find_target_window(title_keyword: str) -> Any | None:
    """Find the largest non-minimized window whose title contains `title_keyword`.

    Falls back to a minimized window if no normal-sized candidate exists; the
    caller is expected to warn the user (WGC cannot capture iconic windows).
    """
    candidates = gw.getWindowsWithTitle(title_keyword)
    normal = [w for w in candidates if not w.isMinimized and w.width > 100 and w.height > 100]
    if normal:
        return max(normal, key=lambda w: w.width * w.height)
    minimized = [w for w in candidates if w.isMinimized]
    if minimized:
        return minimized[0]
    return None


def _is_minimized(hwnd: int) -> bool:
    try:
        return bool(win32gui.IsIconic(hwnd))
    except Exception:  # noqa: BLE001 — pywin32 raises broad types on stale handles
        return False


# ---------------------------------------------------------------------------
# SlideCapture
# ---------------------------------------------------------------------------


class SlideMetadata(TypedDict, total=False):
    type: str  # "slide" or "revisit"
    filename: str
    slide_number: int
    original_slide: int
    timestamp: str
    filepath: str


class SlideCapture:
    """Capture a target window and persist a deduplicated stream of PPT frames.

    Lifecycle:
        cap = SlideCapture(output_dir, title_keyword, interval, threshold)
        cap.start()              # blocks until WGC delivers first frame, then returns
        ...                       # poll loop runs in a background thread
        slides_meta = cap.stop()  # joins thread; returns metadata list

    The metadata list contains one entry per saved slide (type="slide") and
    one stub per detected revisit (type="revisit", references original_slide).
    """

    _DEDUP_THRESHOLD: float = 3.0
    _MAX_MISSES: int = 6  # number of polling intervals before declaring window lost
    _FRAME_WAIT_INTERVALS: int = 60  # 30 s with 0.5 s polling for first frame

    def __init__(
        self,
        output_dir: Path,
        title_keyword: str,
        interval: float = 5.0,
        threshold: float = 5.0,
        on_window_lost: Callable[[], None] | None = None,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._title_keyword = title_keyword
        self._interval = float(interval)
        self._threshold = float(threshold)
        self._on_window_lost = on_window_lost

        self._stop_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame: Image.Image | None = None
        self._poll_thread: threading.Thread | None = None
        self._capture_control: object | None = None
        self._hwnd: int | None = None

        self._slides: list[SlideMetadata] = []
        self._saved_thumbnails: list[tuple[int, np.ndarray]] = []
        self._slide_num: int = 0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._poll_thread is not None:
            raise RuntimeError("SlideCapture already started")

        window = find_target_window(self._title_keyword)
        if window is None:
            raise RuntimeError(
                f"No window matching '{self._title_keyword}'. "
                f"未找到窗口 / open the meeting window first."
            )

        self._hwnd = int(window._hWnd)
        if _is_minimized(self._hwnd):
            print(
                "  [Capture] WARNING: window is minimized — WGC cannot capture iconic windows.",
                flush=True, file=sys.stderr,
            )

        print(
            f"  [Capture] Target: '{window.title}' ({window.width}x{window.height})",
            flush=True, file=sys.stderr,
        )
        self._start_wgc()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop(self) -> list[SlideMetadata]:
        self._stop_event.set()

        if self._capture_control is not None:
            try:
                # CaptureControl exposes .stop() in windows-capture 2.0
                self._capture_control.stop()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                print(f"  [Capture] CaptureControl.stop() raised: {exc!r}", flush=True, file=sys.stderr)
            self._capture_control = None

        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        self._poll_thread = None

        print(f"  [Capture] Stopped. Slides recorded: {len(self._slides)}", flush=True, file=sys.stderr)
        return list(self._slides)

    # ------------------------------------------------------------------ wgc

    def _start_wgc(self) -> None:
        capture = WindowsCapture(
            window_hwnd=self._hwnd,
            cursor_capture=False,
            draw_border=None,
        )

        @capture.event
        def on_frame_arrived(frame: Frame, _control: InternalCaptureControl) -> None:
            # frame_buffer is BGRA H×W×4 uint8; convert to RGB PIL Image
            bgra = frame.frame_buffer
            rgb = bgra[:, :, [2, 1, 0]]
            img = Image.fromarray(rgb)
            with self._frame_lock:
                self._latest_frame = img

        @capture.event
        def on_closed() -> None:
            if not self._stop_event.is_set():
                print("  [Capture] Capture session closed by OS", flush=True, file=sys.stderr)
                if self._on_window_lost is not None:
                    self._on_window_lost()

        self._capture_control = capture.start_free_threaded()

    # ------------------------------------------------------------------ polling

    def _latest(self) -> Image.Image | None:
        with self._frame_lock:
            return self._latest_frame

    def _save_slide(self, img: Image.Image, slide_num: int) -> SlideMetadata:
        filename = f"slide_{slide_num:03d}.png"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        filepath = self._output_dir / filename
        img.save(filepath, "PNG")

        timestamp = datetime.now().strftime("%H:%M:%S")
        meta: SlideMetadata = {
            "type": "slide",
            "filename": filename,
            "slide_number": slide_num,
            "timestamp": timestamp,
            "filepath": str(filepath),
        }
        self._slides.append(meta)
        print(f"  [Slide {slide_num:03d}] saved at {timestamp}", flush=True, file=sys.stderr)
        return meta

    def _record_revisit(self, original: int) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        meta: SlideMetadata = {
            "type": "revisit",
            "original_slide": original,
            "timestamp": timestamp,
        }
        self._slides.append(meta)
        print(f"  [Capture] Revisit of slide {original:03d}", flush=True, file=sys.stderr)

    def _record_thumbnail(self, img: Image.Image, slide_num: int) -> None:
        self._saved_thumbnails.append((slide_num, _to_thumbnail(img)))

    def _poll_loop(self) -> None:
        # Wait up to 30 s for the first WGC frame to arrive.
        for _ in range(self._FRAME_WAIT_INTERVALS):
            if self._stop_event.is_set():
                return
            if self._latest() is not None:
                break
            time.sleep(0.5)
        else:
            print("  [Capture] Timed out waiting for first frame", flush=True, file=sys.stderr)
            if self._on_window_lost is not None:
                self._on_window_lost()
            return

        # Save initial slide.
        first = self._latest()
        assert first is not None
        self._slide_num = 1
        self._save_slide(first, self._slide_num)
        self._record_thumbnail(first, self._slide_num)
        prev_frame = first

        miss_count = 0
        while not self._stop_event.is_set():
            self._stop_event.wait(self._interval)
            if self._stop_event.is_set():
                break

            # Verify the target window is still around.
            window = find_target_window(self._title_keyword)
            if window is None:
                miss_count += 1
                remaining = (self._MAX_MISSES - miss_count) * self._interval
                print(
                    f"  [Capture] Window not found ({miss_count}/{self._MAX_MISSES}), "
                    f"auto-stop in {remaining:.0f}s",
                    flush=True, file=sys.stderr,
                )
                if miss_count >= self._MAX_MISSES:
                    print("  [Capture] Window gone too long, stopping", flush=True, file=sys.stderr)
                    if self._on_window_lost is not None:
                        self._on_window_lost()
                    return
                continue
            if miss_count > 0:
                print("  [Capture] Window recovered", flush=True, file=sys.stderr)
                miss_count = 0
            if _is_minimized(int(window._hWnd)):
                print("  [Capture] WARNING: window is minimized", flush=True, file=sys.stderr)

            current = self._latest()
            if current is None:
                continue

            change = compute_change_ratio(prev_frame, current)
            if change <= self._threshold:
                continue

            print(f"  [Capture] Change: {change:.1f}% > {self._threshold}%", flush=True, file=sys.stderr)
            settled = self._debounce_and_settle(current)
            if settled is None:
                continue

            final_change = compute_change_ratio(prev_frame, settled)
            if final_change <= self._threshold:
                print(
                    f"  [Capture] Settled back to {final_change:.1f}%, treating as transient",
                    flush=True, file=sys.stderr,
                )
                continue

            dup = find_duplicate(settled, self._saved_thumbnails, self._DEDUP_THRESHOLD)
            if dup is not None:
                self._record_revisit(dup)
            else:
                self._slide_num += 1
                self._save_slide(settled, self._slide_num)
                self._record_thumbnail(settled, self._slide_num)
            prev_frame = settled

    def _debounce_and_settle(self, candidate: Image.Image) -> Image.Image | None:
        """Two-stage debounce: wait 0.5 s + 0.5 s, with a 1 s safety net.

        Rejects fly-through transitions and animation peaks by requiring the
        frame to look the same after two short waits.
        """
        time.sleep(0.5)
        stable = self._latest()
        if stable is None:
            return None

        time.sleep(0.5)
        stable2 = self._latest()
        if stable2 is None:
            stable2 = stable

        inner = compute_change_ratio(stable, stable2)
        if inner > 2.0:
            # Still moving; wait another second and re-read.
            time.sleep(1.0)
            latest = self._latest()
            if latest is not None:
                stable2 = latest

        return stable2
