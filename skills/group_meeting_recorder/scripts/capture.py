#!/usr/bin/env python3
"""
Meeting PPT Capture - Auto-detect slide changes and capture screenshots
Designed for Microsoft Teams online meetings
"""

import argparse
import ctypes
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import ctypes.wintypes

import mss
import numpy as np
import pygetwindow as gw
import win32gui
import win32ui
from PIL import Image


# --- DPI Awareness (must be called early) ---
def set_dpi_awareness():
    """Set process DPI awareness for correct coordinates on high-DPI screens"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Fallback
        except Exception:
            pass


set_dpi_awareness()


# --- Global state ---
captured_slides = []
start_time = None
output_dir = None


def find_target_window(title_keyword: str):
    """Find the largest visible window matching the title keyword"""
    windows = gw.getWindowsWithTitle(title_keyword)

    # Filter out minimized windows
    visible = [w for w in windows if not w.isMinimized and w.width > 100 and w.height > 100]

    if not visible:
        return None

    # Return the largest window (meeting window is usually the biggest)
    return max(visible, key=lambda w: w.width * w.height)


def capture_window_region(sct, window) -> Image.Image:
    """Capture the actual window content using Win32 PrintWindow API.

    This captures the window itself, not the screen region,
    so overlapping windows won't appear in the screenshot.
    """
    hwnd = window._hWnd

    # Get window client area dimensions
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    w = right - left
    h = bottom - top

    if w <= 0 or h <= 0:
        # Fallback to screen region capture if client rect is invalid
        monitor = {
            "left": window.left,
            "top": window.top,
            "width": window.width,
            "height": window.height,
        }
        screenshot = sct.grab(monitor)
        return Image.frombytes("RGB", screenshot.size, screenshot.rgb)

    # Create device contexts and bitmap
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()

    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, window.width, window.height)
    save_dc.SelectObject(bitmap)

    # PrintWindow with PW_RENDERFULLCONTENT flag (2) for better capture
    # Flag 2 = PW_RENDERFULLCONTENT, works for DWM-composed windows
    ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)

    # Convert to PIL Image
    bmp_info = bitmap.GetInfo()
    bmp_bits = bitmap.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGB",
        (bmp_info["bmWidth"], bmp_info["bmHeight"]),
        bmp_bits, "raw", "BGRX", 0, 1
    )

    # Cleanup
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    win32gui.DeleteObject(bitmap.GetHandle())

    return img


def compute_change_ratio(img1: Image.Image, img2: Image.Image, pixel_threshold: int = 30) -> float:
    """
    Compute the percentage of pixels that changed between two frames.

    Both images are resized to 640x480 for fast comparison.
    A pixel is considered 'changed' if its mean channel difference > pixel_threshold.
    Returns: float in [0, 100] representing percentage of changed pixels.
    """
    size = (640, 480)
    a = np.array(img1.resize(size), dtype=np.int16)
    b = np.array(img2.resize(size), dtype=np.int16)

    # Mean absolute difference across color channels
    diff = np.abs(a - b).mean(axis=2)

    # Count pixels exceeding threshold
    changed = (diff > pixel_threshold).sum()
    total = size[0] * size[1]

    return (changed / total) * 100.0


def wait_for_stable(sct, window, max_wait: float = 3.0, stable_threshold: float = 2.0) -> Image.Image:
    """
    Wait for the screen to stabilize after detecting a change (debounce).
    Returns the stable frame.
    """
    time.sleep(0.5)
    frame = capture_window_region(sct, window)

    elapsed = 0.5
    while elapsed < max_wait:
        time.sleep(0.5)
        elapsed += 0.5

        # Re-find window in case it moved
        new_window = find_target_window(window.title.split()[0] if window.title else "Teams")
        if new_window is None:
            return frame
        window = new_window

        next_frame = capture_window_region(sct, window)
        change = compute_change_ratio(frame, next_frame)

        if change < stable_threshold:
            return next_frame  # Stable
        frame = next_frame

    return frame  # Return last frame if not stabilized


def save_slide(img: Image.Image, slide_num: int, output_path: Path) -> dict:
    """Save a captured slide and return metadata"""
    filename = f"slide_{slide_num:03d}.png"
    filepath = output_path / filename
    img.save(filepath, "PNG")

    timestamp = datetime.now().strftime("%H:%M:%S")
    metadata = {
        "filename": filename,
        "slide_number": slide_num,
        "timestamp": timestamp,
        "filepath": str(filepath),
    }

    print(f"  [Slide {slide_num:03d}] Saved at {timestamp} -> {filename}")
    return metadata


def output_json_summary():
    """Print JSON summary with marker for Claude to parse"""
    end_time = datetime.now()
    summary = {
        "status": "completed",
        "total_slides": len(captured_slides),
        "start_time": start_time.strftime("%H:%M:%S") if start_time else None,
        "end_time": end_time.strftime("%H:%M:%S"),
        "date": end_time.strftime("%Y-%m-%d"),
        "output_dir": str(output_dir),
        "slides": captured_slides,
    }

    print("\n===JSON_OUTPUT===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("===END_JSON===")


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    print("\n\n[Info] Received interrupt signal, stopping capture...")
    output_json_summary()
    sys.exit(0)


def main():
    global captured_slides, start_time, output_dir

    parser = argparse.ArgumentParser(
        description="Auto-capture Teams meeting slides on page changes"
    )
    parser.add_argument(
        "--threshold", type=float, default=5.0,
        help="Change detection threshold (%% of changed pixels, default: 5.0)"
    )
    parser.add_argument(
        "--interval", type=float, default=5.0,
        help="Detection interval in seconds (default: 5.0)"
    )
    parser.add_argument(
        "--title", type=str, default="Teams",
        help="Window title keyword to match (default: 'Teams')"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output directory (default: ~/Desktop/meeting_captures/YYYY-MM-DD/)"
    )
    args = parser.parse_args()

    # Setup output directory
    today = datetime.now().strftime("%Y-%m-%d")
    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = Path.home() / "Desktop" / "meeting_captures" / today
    output_dir.mkdir(parents=True, exist_ok=True)

    # Register signal handler
    signal.signal(signal.SIGINT, signal_handler)

    print("=" * 60)
    print("  Meeting PPT Capture")
    print("=" * 60)
    print(f"  Window keyword : {args.title}")
    print(f"  Threshold      : {args.threshold}%")
    print(f"  Interval       : {args.interval}s")
    print(f"  Output dir     : {output_dir}")
    print("=" * 60)

    # Wait for target window
    print(f"\n[Info] Searching for window matching '{args.title}'...")
    window = None
    for attempt in range(12):  # Wait up to 60s
        window = find_target_window(args.title)
        if window:
            break
        print(f"  Waiting for window... ({(attempt + 1) * 5}s)")
        time.sleep(5)

    if window is None:
        print(f"[Error] Could not find window matching '{args.title}' after 60s")
        print("  Make sure the Teams meeting window is open and not minimized.")
        sys.exit(1)

    print(f"[OK] Found window: '{window.title}' ({window.width}x{window.height})")
    print(f"     Position: ({window.left}, {window.top})")

    # Start capturing
    start_time = datetime.now()
    slide_num = 0
    miss_count = 0  # Consecutive window-not-found count
    max_misses = 6  # 6 * interval = 30s default

    with mss.mss() as sct:
        # Capture initial frame
        slide_num += 1
        prev_frame = capture_window_region(sct, window)
        metadata = save_slide(prev_frame, slide_num, output_dir)
        captured_slides.append(metadata)

        print(f"\n[Info] Monitoring started at {start_time.strftime('%H:%M:%S')}")
        print(f"  Press Ctrl+C to stop manually.\n")

        while True:
            time.sleep(args.interval)

            # Re-find window each cycle
            current_window = find_target_window(args.title)
            if current_window is None:
                miss_count += 1
                remaining = (max_misses - miss_count) * args.interval
                print(f"  [Warning] Window not found ({miss_count}/{max_misses}), "
                      f"will stop in {remaining:.0f}s if not recovered...")
                if miss_count >= max_misses:
                    print("\n[Info] Window gone for 30s+, assuming meeting ended.")
                    break
                continue
            else:
                if miss_count > 0:
                    print(f"  [OK] Window recovered")
                miss_count = 0
                window = current_window

            # Capture current frame
            try:
                current_frame = capture_window_region(sct, window)
            except Exception as e:
                print(f"  [Warning] Capture failed: {e}")
                miss_count += 1
                continue

            # Compare with previous frame
            change = compute_change_ratio(prev_frame, current_frame)

            if change > args.threshold:
                print(f"  [Detected] Change: {change:.1f}% > {args.threshold}% threshold")

                # Debounce: wait for stable frame
                stable_frame = wait_for_stable(sct, window)

                # Verify it's still different from previous slide
                final_change = compute_change_ratio(prev_frame, stable_frame)
                if final_change > args.threshold:
                    slide_num += 1
                    metadata = save_slide(stable_frame, slide_num, output_dir)
                    captured_slides.append(metadata)
                    prev_frame = stable_frame
                else:
                    print(f"  [Skipped] Change settled back ({final_change:.1f}%), likely transient")
            # else: no significant change, continue monitoring

    # Output summary
    output_json_summary()
    print(f"\n[Done] Captured {len(captured_slides)} slides to {output_dir}")


if __name__ == "__main__":
    main()
