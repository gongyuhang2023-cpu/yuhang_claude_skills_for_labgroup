"""
PPT slide capture using Windows Graphics Capture API.
Captures window content even when occluded or off-screen.
"""

import ctypes
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pygetwindow as gw
from PIL import Image

MEETING_PRESETS = {
    "teams": "Teams",
    "zoom": "Zoom Meeting",
    "tencent": "腾讯会议",
}


def set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


set_dpi_awareness()


def find_target_window(title_keyword: str):
    windows = gw.getWindowsWithTitle(title_keyword)
    # Prefer non-minimized windows with reasonable size
    normal = [w for w in windows if not w.isMinimized and w.width > 100 and w.height > 100]
    if normal:
        return max(normal, key=lambda w: w.width * w.height)
    # Fall back to minimized windows (will be restored by _warn_if_minimized)
    minimized = [w for w in windows if w.isMinimized]
    if minimized:
        return minimized[0]
    return None


THUMB_SIZE = (640, 480)


def compute_change_ratio(img1: Image.Image, img2: Image.Image, pixel_threshold: int = 30) -> float:
    a = np.array(img1.resize(THUMB_SIZE), dtype=np.int16)
    b = np.array(img2.resize(THUMB_SIZE), dtype=np.int16)
    diff = np.abs(a - b).mean(axis=2)
    changed = (diff > pixel_threshold).sum()
    total = THUMB_SIZE[0] * THUMB_SIZE[1]
    return (changed / total) * 100.0


def _compare_thumbnails(thumb_a: np.ndarray, thumb_b: np.ndarray, pixel_threshold: int = 30) -> float:
    """Compare two pre-resized 640x480 numpy arrays. Avoids redundant resize."""
    diff = np.abs(thumb_a.astype(np.int16) - thumb_b.astype(np.int16)).mean(axis=2)
    changed = (diff > pixel_threshold).sum()
    total = thumb_a.shape[0] * thumb_a.shape[1]
    return (changed / total) * 100.0


def _warn_if_minimized(hwnd):
    """Warn if window is minimized — WGC cannot capture minimized windows."""
    import win32gui
    if win32gui.IsIconic(hwnd):
        print("  [Capture] WARNING: Window is minimized, cannot capture. Please restore it.")
        return True
    return False


class SlideCapture:

    def __init__(self, output_dir, title_keyword, interval=5.0, threshold=5.0,
                 on_window_lost=None):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._title_keyword = title_keyword
        self._interval = interval
        self._threshold = threshold
        self._on_window_lost = on_window_lost

        self._stop_event = threading.Event()
        self._latest_frame = None
        self._frame_lock = threading.Lock()
        self._slides = []
        self._slide_num = 0
        self._saved_thumbnails = []  # [(slide_num, numpy_640x480)]
        self._poll_thread = None
        self._capture_control = None
        self._hwnd = None

    def start(self):
        window = find_target_window(self._title_keyword)
        if window is None:
            print(f"  [Capture] Waiting for window '{self._title_keyword}'...")
            for attempt in range(12):
                time.sleep(5)
                window = find_target_window(self._title_keyword)
                if window:
                    break
                print(f"  [Capture] Still waiting... ({(attempt + 1) * 5}s)")

        if window is None:
            raise RuntimeError(
                f"Window matching '{self._title_keyword}' not found after 60s. "
                f"Ensure the meeting window is open."
            )

        self._hwnd = window._hWnd
        _warn_if_minimized(self._hwnd)

        print(f"  [Capture] Found: '{window.title}' ({window.width}x{window.height})")

        self._start_wgc()

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _start_wgc(self):
        from windows_capture import WindowsCapture, Frame, InternalCaptureControl

        capture = WindowsCapture(
            window_hwnd=self._hwnd,
            cursor_capture=False,
            draw_border=None,
        )

        frame_lock = self._frame_lock
        stop_event = self._stop_event
        on_window_lost = self._on_window_lost
        owner = self

        @capture.event
        def on_frame_arrived(frame: Frame, control: InternalCaptureControl):
            bgra = frame.frame_buffer
            rgb = bgra[:, :, [2, 1, 0]]
            img = Image.fromarray(rgb)
            with frame_lock:
                owner._latest_frame = img
            owner._capture_control = control

        @capture.event
        def on_closed():
            if not stop_event.is_set():
                print("  [Capture] Window closed / capture session ended")
                if on_window_lost:
                    on_window_lost()

        self._capture_handle = capture.start_free_threaded()

    def _get_frame(self) -> Image.Image | None:
        with self._frame_lock:
            return self._latest_frame

    def _find_duplicate(self, img: Image.Image, dedup_threshold: float = 3.0) -> int | None:
        """Check if img matches any previously saved slide. Returns original slide_num or None."""
        thumb = np.array(img.resize(THUMB_SIZE), dtype=np.uint8)
        for saved_num, saved_thumb in self._saved_thumbnails:
            change = _compare_thumbnails(thumb, saved_thumb)
            if change < dedup_threshold:
                return saved_num
        return None

    def _record_thumbnail(self, img: Image.Image, slide_num: int):
        thumb = np.array(img.resize(THUMB_SIZE), dtype=np.uint8)
        self._saved_thumbnails.append((slide_num, thumb))

    def _save_slide(self, img: Image.Image, slide_num: int) -> dict:
        filename = f"slide_{slide_num:03d}.png"
        filepath = self._output_dir / filename
        img.save(filepath, "PNG")

        timestamp = datetime.now().strftime("%H:%M:%S")
        metadata = {
            "filename": filename,
            "slide_number": slide_num,
            "timestamp": timestamp,
            "filepath": str(filepath),
        }
        self._slides.append(metadata)
        print(f"  [Slide {slide_num:03d}] Saved at {timestamp}")
        return metadata

    def _poll_loop(self):
        # Wait for first frame from WGC
        for _ in range(60):
            if self._stop_event.is_set():
                return
            frame = self._get_frame()
            if frame is not None:
                break
            time.sleep(0.5)
        else:
            print("  [Capture] Timed out waiting for first frame")
            if self._on_window_lost:
                self._on_window_lost()
            return

        # Save initial slide
        self._slide_num = 1
        prev_frame = self._get_frame()
        self._save_slide(prev_frame, self._slide_num)
        self._record_thumbnail(prev_frame, self._slide_num)

        miss_count = 0
        max_misses = 6

        while not self._stop_event.is_set():
            self._stop_event.wait(self._interval)
            if self._stop_event.is_set():
                break

            # Check window still exists
            window = find_target_window(self._title_keyword)
            if window is None:
                miss_count += 1
                remaining = (max_misses - miss_count) * self._interval
                print(f"  [Capture] Window not found ({miss_count}/{max_misses}), "
                      f"auto-stop in {remaining:.0f}s...")
                if miss_count >= max_misses:
                    print("  [Capture] Window gone for 30s+, stopping.")
                    if self._on_window_lost:
                        self._on_window_lost()
                    return
                continue
            else:
                if miss_count > 0:
                    print("  [Capture] Window recovered")
                miss_count = 0
                _warn_if_minimized(window._hWnd)

            current_frame = self._get_frame()
            if current_frame is None:
                continue

            change = compute_change_ratio(prev_frame, current_frame)

            if change > self._threshold:
                print(f"  [Capture] Change: {change:.1f}% > {self._threshold}%")

                # Debounce: wait for content to stabilize
                time.sleep(0.5)
                stable_frame = self._get_frame()
                if stable_frame is None:
                    continue

                time.sleep(0.5)
                stable_frame2 = self._get_frame()
                if stable_frame2 is None:
                    stable_frame2 = stable_frame

                inner_change = compute_change_ratio(stable_frame, stable_frame2)
                if inner_change > 2.0:
                    time.sleep(1.0)
                    stable_frame2 = self._get_frame() or stable_frame2

                final_change = compute_change_ratio(prev_frame, stable_frame2)
                if final_change > self._threshold:
                    dup = self._find_duplicate(stable_frame2)
                    if dup is not None:
                        self._slides.append({
                            "type": "revisit",
                            "original_slide": dup,
                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                        })
                        print(f"  [Capture] Revisit slide {dup:03d}, skipped saving")
                    else:
                        self._slide_num += 1
                        self._save_slide(stable_frame2, self._slide_num)
                        self._record_thumbnail(stable_frame2, self._slide_num)
                    prev_frame = stable_frame2
                else:
                    print(f"  [Capture] Settled back ({final_change:.1f}%), transient")

    def stop(self) -> list[dict]:
        self._stop_event.set()

        if self._capture_control:
            try:
                self._capture_control.stop()
            except Exception:
                pass

        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)

        print(f"  [Capture] Stopped. Total slides: {len(self._slides)}")
        return self._slides
