#!/usr/bin/env python3
"""
MeetingMind recording session.
Runs audio recording + slide capture in parallel.
Stops on SIGINT (TaskStop) or when the meeting window disappears.
"""

import argparse
import json
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from audio_recorder import WasapiLoopbackRecorder
from slide_capture import SlideCapture, MEETING_PRESETS


class MeetingSession:

    def __init__(self, meeting: str, interval: float, threshold: float, output_dir: str, record_mic: bool = False):
        self._stop_event = threading.Event()
        self._record_mic = record_mic

        self._output_dir = Path(output_dir)
        self._audio_dir = self._output_dir / "audio"
        self._slides_dir = self._output_dir / "slides"
        self._transcript_dir = self._output_dir / "transcript"

        for d in [self._audio_dir, self._slides_dir, self._transcript_dir]:
            d.mkdir(parents=True, exist_ok=True)

        title_keyword = MEETING_PRESETS.get(meeting, meeting)

        self._recorder = WasapiLoopbackRecorder(
            output_path=self._audio_dir / "recording.wav",
            record_mic=record_mic,
        )
        self._capture = SlideCapture(
            output_dir=self._slides_dir,
            title_keyword=title_keyword,
            interval=interval,
            threshold=threshold,
            on_window_lost=self._on_window_lost,
        )

        self._start_time = None
        self._end_time = None
        self._audio_path = None
        self._slides_meta = []

    def _on_window_lost(self):
        print("\n[Session] Meeting window lost, stopping session...")
        self._stop_event.set()

    def start(self):
        self._start_time = datetime.now()

        print(f"\n[Session] Starting at {self._start_time.strftime('%H:%M:%S')}")
        print(f"[Session] Output: {self._output_dir}\n")

        self._recorder.start()

        try:
            self._capture.start()
        except RuntimeError as e:
            print(f"[Error] Capture failed: {e}")
            print("[Session] Continuing with audio-only recording...")

    def wait(self):
        stop_file = self._output_dir / "STOP"
        print(f"\n[Session] Running... (create STOP file or SIGINT to stop)\n")
        while not self._stop_event.is_set():
            if stop_file.exists():
                print("\n[Session] Stop file detected, stopping...")
                stop_file.unlink(missing_ok=True)
                self._stop_event.set()
                break
            self._stop_event.wait(timeout=1.0)

    def stop(self):
        self._end_time = datetime.now()
        print(f"\n[Session] Stopping at {self._end_time.strftime('%H:%M:%S')}...")

        self._slides_meta = self._capture.stop()
        self._audio_path = self._recorder.stop()

        self._write_metadata()
        self._output_json_summary()

        duration = self._end_time - self._start_time
        minutes = int(duration.total_seconds() // 60)
        seconds = int(duration.total_seconds() % 60)
        print(f"\n[Session] Done. Duration: {minutes}m {seconds}s")
        print(f"  Audio: {self._audio_path}")
        print(f"  Slides: {len(self._slides_meta)} captured")
        print(f"  Output: {self._output_dir}")

    def _write_metadata(self):
        metadata = {
            "date": self._start_time.strftime("%Y-%m-%d"),
            "start_time": self._start_time.strftime("%H:%M:%S"),
            "end_time": self._end_time.strftime("%H:%M:%S"),
            "duration_seconds": int((self._end_time - self._start_time).total_seconds()),
            "audio_path": str(self._audio_path) if self._audio_path else None,
            "audio_sample_rate": self._recorder.sample_rate,
            "audio_channels": self._recorder.channels,
            "total_slides": len(self._slides_meta),
            "slides": self._slides_meta,
            "output_dir": str(self._output_dir),
        }
        meta_path = self._output_dir / "metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

    def _output_json_summary(self):
        summary = {
            "status": "completed",
            "date": self._start_time.strftime("%Y-%m-%d"),
            "start_time": self._start_time.strftime("%H:%M:%S"),
            "end_time": self._end_time.strftime("%H:%M:%S"),
            "duration_seconds": int((self._end_time - self._start_time).total_seconds()),
            "audio_path": str(self._audio_path) if self._audio_path else None,
            "total_slides": len(self._slides_meta),
            "slides": self._slides_meta,
            "output_dir": str(self._output_dir),
            "metadata_path": str(self._output_dir / "metadata.json"),
        }
        print("\n===JSON_OUTPUT===")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("===END_JSON===")


def main():
    parser = argparse.ArgumentParser(description="MeetingMind recording session")
    parser.add_argument("--meeting", type=str, default="teams",
                        help="Meeting software: teams, zoom, tencent, or custom window title")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Screenshot detection interval in seconds")
    parser.add_argument("--threshold", type=float, default=5.0,
                        help="Change detection threshold (%%)")
    parser.add_argument("--output", type=str, required=True,
                        help="Output directory for this session")
    parser.add_argument("--mic", action="store_true",
                        help="Also record microphone input (mixed with system audio)")
    args = parser.parse_args()

    session = MeetingSession(
        meeting=args.meeting,
        interval=args.interval,
        threshold=args.threshold,
        output_dir=args.output,
        record_mic=args.mic,
    )

    def handle_signal(sig, frame):
        print("\n[Session] Received stop signal...")
        session._stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("=" * 60)
    print("  MeetingMind Recording Session")
    print("=" * 60)
    print(f"  Meeting    : {args.meeting}")
    print(f"  Interval   : {args.interval}s")
    print(f"  Threshold  : {args.threshold}%")
    print(f"  Mic record : {'Yes' if args.mic else 'No'}")
    print(f"  Output     : {args.output}")
    print("=" * 60)

    try:
        session.start()
        session.wait()
    except Exception as e:
        print(f"\n[Error] {e}")
    finally:
        session.stop()


if __name__ == "__main__":
    main()
