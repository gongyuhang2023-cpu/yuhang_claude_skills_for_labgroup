"""Opt-in Windows Teams capture endpoint and recording-scoped monitoring."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from threading import Event
from time import monotonic

TEAMS_ALIASES = frozenset({"teams", "ms-teams", "ms-teams.exe"})
_MONITOR_BLOCK = 2400  # 50 ms at 48 kHz
_RETRY_SECONDS = 0.5


def capture_device_id() -> str | None:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    path = Path(local_appdata) / "MeetingMind" / "teams-audio.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read Teams audio configuration {path}: {exc}") from exc
    device_id = config.get("capture_device_id") if isinstance(config, dict) else None
    if not isinstance(device_id, str) or not device_id.strip():
        raise RuntimeError(f"{path} requires capture_device_id (the full Windows render endpoint ID)")
    return device_id


def prepare_wasapi() -> None:
    # Load soundcard on the calling thread before pycaw/other COM users.
    # Importing pythoncom first on a worker would implicitly initialize its STA.
    import soundcard  # noqa: F401
    import pythoncom  # noqa: F401


@contextlib.contextmanager
def wasapi_thread():
    import pythoncom

    pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def loopback(device_id: str):
    import soundcard as sc

    # get_microphone() can fall back to name/substring matching. Use only an
    # exact ID among render loopbacks, even if another endpoint has the same name.
    for mic in sc.all_microphones(include_loopback=True):
        if mic.isloopback and mic.id == device_id:
            return mic
    raise RuntimeError(f"Configured Teams render loopback unavailable: {device_id!r}")


class MonitorFeedbackError(RuntimeError):
    """The default output is the capture endpoint."""


def monitor_speaker(source_id: str):
    import soundcard as sc

    speaker = sc.default_speaker()
    if speaker.id == source_id:
        raise MonitorFeedbackError(
            f"Windows default output is the Teams capture endpoint ({source_id}). "
            "Select a physical speaker or headset as Windows default output."
        )
    return speaker


def report(message: str) -> None:
    with contextlib.suppress(OSError, UnicodeError):
        print(f"[teams-audio] {message}", file=sys.stderr, flush=True)


def monitor(source_id: str, stop: Event, samplerate: int, channels: int) -> None:
    """One soundcard reader/player per source in the Windows logon session."""
    try:
        import win32event

        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
        mutex = win32event.CreateMutex(None, False, f"Local\\MeetingMind.TeamsAudio.{digest}")
        try:
            with wasapi_thread():
                while not stop.is_set():
                    result = win32event.WaitForSingleObject(mutex, 0)
                    if result == win32event.WAIT_TIMEOUT:
                        stop.wait(_RETRY_SECONDS)
                        continue
                    if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                        raise RuntimeError(f"Unexpected audio monitor mutex result: {result}")
                    try:
                        _play_loopback(source_id, stop, samplerate, channels)
                    finally:
                        win32event.ReleaseMutex(mutex)
                    break
        finally:
            mutex.Close()
    except Exception as exc:
        report(f"Listening monitor stopped: {exc}")


def _play_loopback(source_id: str, stop: Event, samplerate: int, channels: int) -> None:
    stream_options = dict(samplerate=samplerate, channels=channels, blocksize=_MONITOR_BLOCK)
    last_error = None
    while not stop.is_set():
        try:
            speaker = monitor_speaker(source_id)
            mic = loopback(source_id)
            with mic.recorder(**stream_options) as rec, speaker.player(**stream_options) as player:
                report(f"Listening on Windows default output: {speaker.name} ({speaker.id})")
                next_check = monotonic() + _RETRY_SECONDS
                while not stop.is_set():
                    if monotonic() >= next_check:
                        if monitor_speaker(source_id).id != speaker.id:
                            break
                        next_check = monotonic() + _RETRY_SECONDS
                    chunk = rec.record(numframes=_MONITOR_BLOCK)
                    if not stop.is_set() and chunk is not None and len(chunk):
                        player.play(chunk)
                        last_error = None
        except Exception as exc:
            if not stop.is_set() and str(exc) != last_error:
                last_error = str(exc)
                report(f"Listening paused: {exc}")
            stop.wait(_RETRY_SECONDS)
