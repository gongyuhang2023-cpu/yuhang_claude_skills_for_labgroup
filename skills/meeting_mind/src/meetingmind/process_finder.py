"""Find Windows processes by name or by active audio session.

The public API:
  - `PROCESS_KEYWORDS`: keyword → list of executable name(s) preset table
  - `find_audio_sessions()`: list audio-relevant processes + active flag
  - `find_by_keyword(kw)`: list all processes matching keyword/preset + active flag

`has_active_session=True` means the process currently holds a WASAPI audio
session (i.e. it is — or recently was — producing sound). This is the
strongest hint for picking the right PID among multi-process apps like Teams.
"""

from __future__ import annotations

from typing import Final, TypedDict

import psutil
from pycaw.pycaw import AudioUtilities

# Map a friendly keyword to the executable names the app is known to use.
# Multi-PID apps (e.g. Teams) list all observed executables.
PROCESS_KEYWORDS: Final[dict[str, list[str]]] = {
    "teams": ["ms-teams.exe", "teams.exe"],
    "zoom": ["zoom.exe", "zoommtg.exe"],
    "tencent": ["wemeetapp.exe", "wemeet.exe"],
    "edge": ["msedge.exe"],
    "chrome": ["chrome.exe"],
    "firefox": ["firefox.exe"],
}

# Apps commonly producing audio. Shown by `find_audio_sessions` even when
# inactive, so the user can preview what they could record.
_KNOWN_MEDIA_APPS: Final[set[str]] = {
    "ms-teams.exe", "teams.exe",
    "zoom.exe", "zoommtg.exe",
    "wemeetapp.exe", "wemeet.exe",
    "msedge.exe", "chrome.exe", "firefox.exe",
    "spotify.exe", "vlc.exe", "wmplayer.exe",
    "discord.exe", "slack.exe", "obs64.exe", "obs.exe",
    "musicbee.exe", "foobar2000.exe",
}


class ProcessInfo(TypedDict):
    pid: int
    name: str
    has_active_session: bool


def _audio_sessions() -> list:
    """Return pycaw audio sessions; broken out for testability.

    pycaw triggers COM initialization which can fail on non-Windows or in
    constrained environments. Callers must tolerate exceptions.
    """
    return AudioUtilities.GetAllSessions()


def _active_session_pids() -> set[int]:
    """Return PIDs that currently hold a WASAPI audio session.

    Returns an empty set if pycaw fails (e.g. COM init error). This degrades
    `has_active_session` to always-False rather than crashing the caller.
    """
    pids: set[int] = set()
    try:
        sessions = _audio_sessions()
    except Exception:  # noqa: BLE001 — pycaw raises broad COM errors
        return pids

    for session in sessions:
        proc = getattr(session, "Process", None)
        if proc is None:
            continue
        try:
            pids.add(int(proc.pid))
        except (AttributeError, ValueError):
            # Process died between enumeration and access, or attribute missing
            continue
    return pids


def _iter_processes() -> list[tuple[int, str]]:
    """Yield (pid, name) for every running process; tolerant of races."""
    result: list[tuple[int, str]] = []
    try:
        iterator = psutil.process_iter(["pid", "name"])
    except Exception:  # noqa: BLE001
        return result

    try:
        for proc in iterator:
            try:
                info = proc.info
                pid = info.get("pid")
                name = info.get("name") or ""
                if pid is not None:
                    result.append((int(pid), str(name)))
            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, ValueError):
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        # process_iter raised mid-iteration; return what we collected so far
        pass

    return result


def find_audio_sessions() -> list[ProcessInfo]:
    """List processes either producing audio now or known to be audio-capable.

    Sort order: active sessions first, then alphabetical by name, then PID.
    """
    active = _active_session_pids()
    results: list[ProcessInfo] = []

    for pid, name in _iter_processes():
        name_lower = name.lower()
        is_active = pid in active
        is_known = name_lower in _KNOWN_MEDIA_APPS
        if not (is_active or is_known):
            continue
        results.append({
            "pid": pid,
            "name": name,
            "has_active_session": is_active,
        })

    results.sort(key=lambda p: (not p["has_active_session"], p["name"].lower(), p["pid"]))
    return results


def find_by_keyword(keyword: str) -> list[ProcessInfo]:
    """List processes matching `keyword`, with audio-session flag.

    `keyword` is matched as either a preset key (teams/zoom/tencent/edge/...)
    or a case-insensitive substring against the executable name.
    """
    kw = keyword.lower().strip()
    if not kw:
        return []

    preset = PROCESS_KEYWORDS.get(kw)
    targets = {t.lower() for t in (preset or [kw])}

    active = _active_session_pids()
    results: list[ProcessInfo] = []

    for pid, name in _iter_processes():
        name_lower = name.lower()
        # Exact match against preset names, OR substring match for custom keyword
        if preset is not None:
            matched = name_lower in targets
        else:
            matched = any(t in name_lower for t in targets)
        if not matched:
            continue
        results.append({
            "pid": pid,
            "name": name,
            "has_active_session": pid in active,
        })

    results.sort(key=lambda p: (not p["has_active_session"], p["pid"]))
    return results


def root_audio_pid(pid: int, *, _max_hops: int = 32) -> int:
    """Climb to the top-most ancestor sharing `pid`'s executable name.

    WASAPI process-loopback (proctap) captures the target PID *and its whole
    descendant tree* (PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE). For
    multi-process apps the OS/pycaw frequently attributes the audio session to
    a *leaf* helper process whose own subtree renders nothing — the real audio
    lives in a *sibling* subtree. Concretely, new Teams (`ms-teams.exe`) holds
    the session on a leaf ms-teams.exe child, but renders meeting audio in
    `msedgewebview2.exe` processes that hang off the *root* ms-teams.exe. The
    leaf and the WebView2 host are siblings, so capturing the leaf's tree yields
    pure silence (validated 2026-06-08 / 06-15: both Teams recordings all-zero).

    Climbing to the top-most ancestor of the *same executable* and capturing
    that makes the tree-include grab every descendant — including the real
    audio renderer. The rooted target's tree is always a *superset* of the
    leaf's, so this can never capture less audio than before (Edge, which
    already worked at the leaf, still works rooted).

    Best-effort: any psutil failure (process vanished, access denied, cycle)
    returns the best PID found so far and never raises.
    """
    try:
        proc = psutil.Process(pid)
        target_name = proc.name().lower()
    except Exception:  # noqa: BLE001 — process gone / access denied
        return pid

    best = pid
    current = proc
    for _ in range(_max_hops):  # bounded so a pathological parent cycle can't hang
        try:
            parent = current.parent()
            if parent is None or parent.name().lower() != target_name:
                break
            best = parent.pid
            current = parent
        except Exception:  # noqa: BLE001 — race during the climb; keep best so far
            break
    return best
