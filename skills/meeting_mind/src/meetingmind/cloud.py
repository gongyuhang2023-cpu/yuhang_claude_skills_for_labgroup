"""The lab cloud (Nextcloud): sign in once, look up, upload.

Which cloud, and which folder holds the meetings, is not written here: the
skill is published, so `cloud login` asks the lab server (`meetingmind-asr
lab` over ssh) the first time and keeps the answer in the local config.

Signing in uses Nextcloud's own Login Flow v2: the user approves "meeting_mind"
in the browser, Nextcloud hands back an app password, and that goes into the
OS keychain (Windows Credential Manager / macOS Keychain) — never into a file.
It shows up, and can be revoked, under Settings → Security → Devices & sessions.

Everything after that is plain WebDAV against the user's own view of the
files (`/remote.php/dav/files/<user>/...`), so it works whether or not the
desktop sync client is installed, or what it happens to sync.

The server sits behind Cloudflare, which refuses request bodies over 100 MB:
files above `CHUNK_THRESHOLD` go up through Nextcloud's chunked upload.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import platform
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

KEYRING_PREFIX = "meetingmind-nextcloud:"
#: Cloudflare's request-body limit is 100 MB; stay well clear of it.
CHUNK_THRESHOLD = 90 * 1024 * 1024
CHUNK_SIZE = 50 * 1024 * 1024
LOGIN_TIMEOUT_S = 20 * 60  # a Login Flow v2 token lives 20 minutes

_DAV = "{DAV:}"
_PROPFIND = (
    b'<?xml version="1.0"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
    b"<d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/><oc:size/></d:prop>"
    b"</d:propfind>"
)


class CloudError(RuntimeError):
    """The cloud said no, or could not be reached. `status` is the HTTP status, if any."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _user_agent() -> str:
    # Nextcloud names the app password after this, so the user can tell it apart.
    return f"meeting_mind skill ({platform.node() or platform.system()})"


Transport = Callable[..., tuple[int, bytes, dict[str, str]]]


def http(method: str, url: str, *, data: bytes | None = None,
         headers: dict[str, str] | None = None, auth: tuple[str, str] | None = None,
         timeout: float = 120) -> tuple[int, bytes, dict[str, str]]:
    """One HTTP request → (status, body, headers). Raises CloudError only when unreachable."""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", _user_agent())
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"cannot reach {url.split('/remote.php')[0]}: {exc}"
                         f" / 连不上云盘：{exc}") from exc


# --------------------------------------------------------------------- sign-in


def login_flow(url: str, *, transport: Transport = http,
               show: Callable[[str], None] | None = None,
               sleep: Callable[[float], None] = time.sleep,
               timeout_s: float = LOGIN_TIMEOUT_S) -> tuple[str, str, str]:
    """Run Nextcloud Login Flow v2 → (server, login name, app password).

    `show` receives the URL the user must open; it is also printed, so an AI
    running this in the background can pass it on.
    """
    status, body, _ = transport("POST", f"{url.rstrip('/')}/index.php/login/v2", data=b"")
    if status != 200:
        raise CloudError(f"the cloud refused to start a login (HTTP {status})", status)
    flow = json.loads(body)
    login_url, poll = flow["login"], flow["poll"]
    print(login_url, flush=True)
    print("Open the link above, sign in and allow access for meeting_mind."
          " / 打开上面的链接，登录后点「允许访问」。", file=sys.stderr, flush=True)
    if show:
        show(login_url)
    waited = 0.0
    form = urllib.parse.urlencode({"token": poll["token"]}).encode()
    while waited < timeout_s:
        status, body, _ = transport("POST", poll["endpoint"], data=form,
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
        if status == 200:
            done = json.loads(body)
            return done["server"].rstrip("/"), done["loginName"], done["appPassword"]
        if status != 404:  # 404 = not approved yet
            raise CloudError(f"login polling failed (HTTP {status})", status)
        sleep(2.0)
        waited += 2.0
    raise CloudError("nobody approved the login within 20 minutes / 20 分钟内没有完成授权，请重试")


def user_id(server: str, login: str, password: str, *, transport: Transport = http) -> str:
    """The account id WebDAV paths use — not always the name one logs in with (SSO)."""
    status, body, _ = transport("GET", f"{server}/ocs/v2.php/cloud/user?format=json",
                                headers={"OCS-APIRequest": "true"}, auth=(login, password))
    if status != 200:
        raise CloudError(f"the cloud did not accept the new app password (HTTP {status})", status)
    return str(json.loads(body)["ocs"]["data"]["id"])


def _keyring() -> Any:
    try:
        import keyring  # noqa: PLC0415 — only cloud features need it
    except ImportError as exc:
        raise CloudError("the 'keyring' package is missing — re-run install.py"
                         " / 缺少 keyring 包，请重跑 install.py") from exc
    return keyring


def save_password(server: str, login: str, password: str) -> None:
    _keyring().set_password(KEYRING_PREFIX + server, login, password)


def load_password(server: str, login: str) -> str | None:
    return _keyring().get_password(KEYRING_PREFIX + server, login)


def forget_password(server: str, login: str) -> None:
    with contextlib.suppress(Exception):
        _keyring().delete_password(KEYRING_PREFIX + server, login)


def display_name(server: str, login: str, password: str, *, transport: Transport = http) -> str:
    """The name lab members know the account by, for "who uploaded this"."""
    with contextlib.suppress(CloudError, ValueError, KeyError):
        status, body, _ = transport("GET", f"{server}/ocs/v2.php/cloud/user?format=json",
                                    headers={"OCS-APIRequest": "true"}, auth=(login, password))
        if status == 200:
            return str(json.loads(body)["ocs"]["data"].get("display-name") or login)
    return login


# --------------------------------------------------------------------- WebDAV


@dataclass(frozen=True)
class Entry:
    name: str
    path: str  # relative to the user's root, starting with "/"
    is_dir: bool
    size: int
    modified: float  # epoch seconds, 0 if unknown


#: Inside `Cloud`, `list` names the method; annotations there use this instead.
Entries = list[Entry]


@dataclass
class Cloud:
    """The signed-in user's view of the cloud. Paths are user-relative, e.g. "/lab/meetings"."""

    server: str
    user: str
    login: str
    password: str
    transport: Transport = http

    @property
    def _root(self) -> str:
        return f"{self.server}/remote.php/dav/files/{urllib.parse.quote(self.user)}"

    def url(self, path: str) -> str:
        return self._root + urllib.parse.quote("/" + path.strip("/"))

    def _call(self, method: str, path_or_url: str, *, ok: tuple[int, ...],
              data: bytes | None = None, headers: dict[str, str] | None = None,
              timeout: float = 120) -> tuple[int, bytes]:
        url = path_or_url if path_or_url.startswith("http") else self.url(path_or_url)
        status, body, _ = self.transport(method, url, data=data, headers=headers,
                                         auth=(self.login, self.password), timeout=timeout)
        if status == 401:
            raise CloudError("the cloud no longer accepts the saved app password — run "
                             "`meetingmind cloud login` again / 云盘授权失效，请重新运行 cloud login",
                             status)
        if status not in ok:
            raise CloudError(f"{method} {path_or_url} → HTTP {status}", status)
        return status, body

    def list(self, path: str) -> Entries:
        """Direct children of `path` (a folder)."""
        _, body = self._call("PROPFIND", path, ok=(207,), data=_PROPFIND,
                             headers={"Depth": "1", "Content-Type": "application/xml"})
        base = urllib.parse.urlparse(self._root).path
        entries = []
        for resp in ET.fromstring(body).findall(f"{_DAV}response"):
            href = urllib.parse.unquote(resp.findtext(f"{_DAV}href") or "")
            rel = "/" + href[len(base):].strip("/") if href.startswith(base) else href
            if rel.rstrip("/") == "/" + path.strip("/"):
                continue  # the folder itself
            prop = resp.find(f"{_DAV}propstat/{_DAV}prop")
            if prop is None:
                continue
            is_dir = prop.find(f"{_DAV}resourcetype/{_DAV}collection") is not None
            size_text = prop.findtext("{http://owncloud.org/ns}size") or prop.findtext(
                f"{_DAV}getcontentlength") or "0"
            stamp = prop.findtext(f"{_DAV}getlastmodified")
            entries.append(Entry(
                name=rel.rstrip("/").rsplit("/", 1)[-1],
                path=rel.rstrip("/"),
                is_dir=is_dir,
                size=int(size_text) if size_text.isdigit() else 0,
                modified=parsedate_to_datetime(stamp).timestamp() if stamp else 0.0,
            ))
        return entries

    def exists(self, path: str) -> bool:
        status, _ = self._call("PROPFIND", path, ok=(207, 404), data=_PROPFIND,
                               headers={"Depth": "0", "Content-Type": "application/xml"})
        return status == 207

    def get(self, path: str) -> bytes:
        return self._call("GET", path, ok=(200,), timeout=600)[1]

    def download(self, path: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(self.get(path))
        tmp.replace(target)

    def mkdir(self, path: str) -> None:
        """Create `path` and any missing parents; existing folders are fine."""
        parts = [p for p in path.strip("/").split("/") if p]
        for i in range(1, len(parts) + 1):
            self._call("MKCOL", "/" + "/".join(parts[:i]), ok=(201, 405))  # 405 = exists

    def claim(self, path: str) -> bool:
        """Create `path` as a new folder; False when it already exists.

        This is the one atomic step two simultaneous uploaders can race on:
        whoever's MKCOL gets 201 owns the folder, the other sees 405 and must
        pick another name — instead of both writing into the same place.
        """
        parent = path.rstrip("/").rsplit("/", 1)[0]
        if parent:
            self.mkdir(parent)
        status, _ = self._call("MKCOL", path, ok=(201, 405))
        return status == 201

    def put_bytes(self, path: str, data: bytes) -> None:
        self._call("PUT", path, ok=(200, 201, 204), data=data)

    def upload(self, local: Path, path: str) -> None:
        """Upload one file; big ones go through Nextcloud's chunked upload (v2)."""
        size = local.stat().st_size
        if size <= CHUNK_THRESHOLD:
            self.put_bytes(path, local.read_bytes())
            return
        dest = {"Destination": self.url(path)}
        staging = (f"{self.server}/remote.php/dav/uploads/{urllib.parse.quote(self.user)}"
                   f"/meetingmind-{uuid.uuid4().hex}")
        self._call("MKCOL", staging, ok=(201,), headers=dest)
        with local.open("rb") as fh:
            for index in range(1, 10_001):
                block = fh.read(CHUNK_SIZE)
                if not block:
                    break
                self._call("PUT", f"{staging}/{index:05d}", ok=(201, 204), data=block,
                           headers={**dest, "OC-Total-Length": str(size)}, timeout=900)
        self._call("MOVE", f"{staging}/.file", ok=(201, 204),
                   headers={**dest, "OC-Total-Length": str(size)}, timeout=900)

    def delete(self, path: str) -> None:
        self._call("DELETE", path, ok=(204, 404))

    def list_or_empty(self, path: str) -> Entries:
        try:
            return self.list(path)
        except CloudError as exc:
            if exc.status == 404:
                return []
            raise


def web_url(server: str, path: str) -> str:
    return f"{server}/apps/files/files?dir={urllib.parse.quote(path)}"


# --------------------------------------------------------------------- meetings
#
# The shared folder holds one directory per meeting: the local meeting
# directory, uploaded whole (some older ones sit a level deeper, e.g.
# "20260727/2026-07-27-group-meeting"). Its metadata.json carries the date and
# the wall-clock span, and that is what identifies a meeting: two people who
# record the same talk name their folders differently, but the clock agrees.

UPLOAD_MARKER = "cloud_upload.json"
#: Where this meeting's own upload went, kept next to it locally. Lets a rerun
#: resume into the same cloud folder even when it was interrupted before the
#: cloud copy had a metadata.json to recognise it by.
LOCAL_RECORD = "cloud_upload.local.json"
#: Two recordings are the same meeting when their spans share at least this
#: much of the shorter one.
SAME_MEETING = 0.5
_DATE_IN_NAME = re.compile(r"(20\d\d)-?([01]\d)-?([0-3]\d)")
_UNSAFE_NAME = re.compile(r"[^\w.-]+")
#: "**我**: " — the speaker label write_transcript puts in front of two-stream text.
_SPEAKER_LABEL = re.compile(r"^\*\*[^*\n]+\*\*:\s*")
#: Uploaded as they are, when present (relative to the meeting directory).
_UPLOAD_FILES = ("metadata.json", "ai_input.json", "summary.md", "interpretation.md",
                 "transcript/transcript.md", "transcript/source.json")
#: Audio goes up as 16 kHz mono FLAC: about 5% of the WAV, and exactly what
#: the speech model consumes, so it can still be transcribed again.
_AUDIO = (("recording.wav", "recording.flac"), ("mic.wav", "mic.flac"))


def name_date(name: str) -> str | None:
    """The first YYYY-MM-DD or YYYYMMDD in `name`, as YYYY-MM-DD."""
    match = _DATE_IN_NAME.search(name)
    return f"{match[1]}-{match[2]}-{match[3]}" if match else None


def normalize_date(value: Any) -> str | None:
    """metadata dates in either spelling compare equal (2026-10-23 == 20261023)."""
    return name_date(value) if isinstance(value, str) else None


def clock_seconds(hms: Any) -> int | None:
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        h, m, s = (int(x) for x in str(hms).split(":"))
        return h * 3600 + m * 60 + s
    return None


def overlap_share(a_start: float, a_len: float, b_start: float, b_len: float) -> float:
    """How much of the shorter span the two spans share (0..1)."""
    shorter = min(a_len, b_len)
    if shorter <= 0:
        return 0.0
    shared = min(a_start + a_len, b_start + b_len) - max(a_start, b_start)
    return max(0.0, shared) / shorter


def safe_name(text: str) -> str:
    """One path segment: no separators, never "." or ".."."""
    return _UNSAFE_NAME.sub("-", text).strip("-.") or "unknown"


def notes_folder(display: str, login: str) -> str:
    """Subfolder for one user's notes inside someone else's meeting.

    The display name keeps it readable; the login hash keeps two people who
    share a display name from writing into the same folder.
    """
    return f"{safe_name(display)}-{hashlib.sha1(login.encode()).hexdigest()[:6]}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class RemoteMeeting:
    path: str                       # user-relative cloud path of the meeting folder
    name: str
    date: str | None
    start_time: str | None = None
    duration: float | None = None
    overlap: float | None = None    # None: no metadata to compare against
    files: set[str] = field(default_factory=set)  # top-level names; folders end in "/"
    uploader: str | None = None
    uploader_login: str | None = None
    upload_status: str | None = None
    self_voice: bool | None = None  # does it contain its recorder's own voice?

    @property
    def same_meeting(self) -> bool:
        return self.overlap is not None and self.overlap >= SAME_MEETING

    @property
    def has_transcript(self) -> bool:
        return "ai_input.json" in self.files or "transcript/" in self.files

    @property
    def complete(self) -> bool:
        """False while someone's upload is still under way (no marker = uploaded by hand)."""
        return self.upload_status in (None, "complete")

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "name": self.name, "date": self.date,
            "start_time": self.start_time, "duration_seconds": self.duration,
            "overlap": None if self.overlap is None else round(self.overlap, 2),
            "same_meeting": self.same_meeting,
            "upload_complete": self.complete,
            "has_transcript": self.has_transcript,
            "has_interpretation": "interpretation.md" in self.files,
            "has_summary": "summary.md" in self.files,
            "has_slides": "slides/" in self.files,
            "uploader": self.uploader, "upload_status": self.upload_status,
            "self_voice_included": self.self_voice,
        }


def _names(entries: list[Entry]) -> set[str]:
    return {e.name + ("/" if e.is_dir else "") for e in entries}


def _get_json(cloud: Cloud, path: str) -> dict[str, Any] | None:
    """A JSON object from the cloud, or None if missing, unreadable or not an object.

    Anyone in the lab can write to the shared folder: one malformed file must
    cost one candidate, not the whole lookup.
    """
    with contextlib.suppress(CloudError, ValueError, UnicodeDecodeError):
        data = json.loads(cloud.get(path))
        return data if isinstance(data, dict) else None
    return None


def _inspect(cloud: Cloud, entry: Entry, files: set[str], meta: dict[str, Any]) -> RemoteMeeting:
    remote = RemoteMeeting(path=entry.path, name=entry.name, date=name_date(entry.name), files=files)
    theirs = _get_json(cloud, f"{entry.path}/metadata.json")
    if theirs is not None:
        remote.date = normalize_date(theirs.get("date")) or remote.date
        start = theirs.get("start_time")
        remote.start_time = start if isinstance(start, str) else None
        with contextlib.suppress(TypeError, ValueError):
            remote.duration = float(theirs.get("duration_seconds") or 0) or None
        # Loopback records what the computer plays: the recorder's own voice is
        # only in it when the microphone was recorded too, or is the source.
        remote.self_voice = theirs.get("mic_captured") is True or theirs.get("audio_mode") == "mic"
        mine, other = clock_seconds(meta.get("start_time")), clock_seconds(remote.start_time)
        with contextlib.suppress(TypeError, ValueError):
            if (remote.date == normalize_date(meta.get("date")) and mine is not None
                    and other is not None and remote.duration):
                remote.overlap = overlap_share(mine, float(meta.get("duration_seconds") or 0),
                                               other, remote.duration)
    if UPLOAD_MARKER in files:
        marker = _get_json(cloud, f"{entry.path}/{UPLOAD_MARKER}") or {}
        remote.uploader = marker.get("uploaded_by") if isinstance(marker.get("uploaded_by"), str) else None
        remote.uploader_login = marker.get("login") if isinstance(marker.get("login"), str) else None
        status = marker.get("status")
        remote.upload_status = status if isinstance(status, str) else "unknown"
    return remote


def find_meetings(cloud: Cloud, folder: str, meta: dict[str, Any]) -> list[RemoteMeeting]:
    """Meetings in `folder` from the same day as `meta`: the same meeting first.

    Same-day folders with a metadata.json are compared by wall-clock span and
    kept when they overlap at all; same-day folders without one (uploaded by
    hand) come last, with overlap None, for the user to judge. A meeting that
    crosses midnight is only found from the day it started on.
    """
    date = normalize_date(meta.get("date")) or ""
    found: list[RemoteMeeting] = []
    for entry in cloud.list_or_empty(folder):
        if not entry.is_dir or (name_date(entry.name) or date) != date:
            continue
        children = cloud.list(entry.path)
        files = _names(children)
        if "metadata.json" in files:
            found.append(_inspect(cloud, entry, files, meta))
            continue
        inner = [
            _inspect(cloud, sub, sub_files, meta)
            for sub in children
            if sub.is_dir and (name_date(sub.name) or date) == date
            and "metadata.json" in (sub_files := _names(cloud.list(sub.path)))
        ]
        if inner:
            found.extend(inner)
        elif name_date(entry.name) == date:
            found.append(RemoteMeeting(path=entry.path, name=entry.name, date=date, files=files))
    kept = [m for m in found if m.date == date and (m.overlap is None or m.overlap > 0)]
    return sorted(kept, key=lambda m: (m.overlap is None, -(m.overlap or 0.0)))


def parse_transcript_md(text: str) -> list[Any]:
    """`[HH:MM:SS] text` paragraphs → Segments (each ends where the next begins).

    Accepts the signed `[-HH:MM:SS]` that `write_signed_transcript` uses.
    """
    from .transcribe import Segment  # noqa: PLC0415

    marks = [((-1 if sign else 1) * (int(h) * 3600 + int(m) * 60 + int(s)),
              _SPEAKER_LABEL.sub("", body.strip()))
             for sign, h, m, s, body in re.findall(
                 r"^\[(-?)(\d\d):(\d\d):(\d\d)\] (.+?)(?=\n\[|\Z)", text, re.S | re.M)]
    return [Segment(float(start), float(marks[i + 1][0] if i + 1 < len(marks) else start + 180),
                    body)
            for i, (start, body) in enumerate(marks) if body]


def signed_clock(seconds: float) -> str:
    """HH:MM:SS with a leading "-" before this recording's start (not clamped to zero)."""
    sign, total = ("-" if seconds < 0 else ""), int(abs(seconds))
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def write_signed_transcript(segments: list[Any], path: Path) -> None:
    """transcript.md for a reused transcript, whose start can lie before this
    recording began: `[-00:05:00]` instead of the usual clamp to 00:00:00,
    which would print several paragraphs all stamped 00:00:00."""
    body = "# 会议转录\n\n" + "".join(f"[{signed_clock(s.start)}] {s.text}\n\n" for s in segments)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _their_segments(cloud: Cloud, path: str) -> list[Any]:
    """Their transcript: ai_input.json's segments, else transcript.md, else an error."""
    from .transcribe import Segment  # noqa: PLC0415

    ai = _get_json(cloud, f"{path}/ai_input.json") or {}
    with contextlib.suppress(KeyError, TypeError, ValueError):
        segments = [Segment(float(s["start_seconds"]), float(s["end_seconds"]), str(s["text"]))
                    for s in ai.get("transcript_segments") or []]
        if segments:
            return segments
    try:
        segments = parse_transcript_md(cloud.get(f"{path}/transcript/transcript.md").decode("utf-8"))
    except CloudError as exc:
        if exc.status != 404:
            raise
        segments = []
    if not segments:
        raise CloudError("that cloud meeting has no transcript text to reuse"
                         " / 云盘那份上传里没有可用的转录文字")
    return segments


def _require_complete(cloud: Cloud, remote: str) -> dict[str, Any] | None:
    marker = _get_json(cloud, f"{remote}/{UPLOAD_MARKER}")
    if marker is not None and marker.get("status") != "complete":
        raise CloudError("that meeting is still being uploaded — try again in a few minutes"
                         " / 那场会还在上传中，过几分钟再试")
    return marker


def reuse_transcript(cloud: Cloud, remote: str, meeting_dir: Path,
                     uploader: str | None = None, *, force: bool = False) -> dict[str, Any]:
    """Build this meeting's transcript.md + ai_input.json from a cloud copy of the same meeting.

    Their timestamps count from *their* start; shifting by the difference in
    wall-clock start puts them on this recording's timeline, next to this
    recording's own slides. Chunks are minutes long, so clock drift of a few
    seconds between two laptops does not matter. Speaker labels are dropped:
    "我" in their transcript is them, not the user.

    Refuses to replace a transcript this meeting already has unless `force`,
    and then moves the old files into transcript/backup-<time>/ first.
    """
    from .postprocess import build_ai_input, load_metadata  # noqa: PLC0415

    mine = load_metadata(meeting_dir)
    tdir = meeting_dir / "transcript"
    own = [p for p in (tdir / "transcript.md", meeting_dir / "ai_input.json", tdir / "source.json")
           if p.is_file()]
    if own and not force:
        raise CloudError("this meeting already has a transcript — pass --force to replace it "
                         "(the current one is backed up) / 这场会已经有转录了；确定要换成云盘上的，加 --force（旧的会备份）")
    _require_complete(cloud, remote)
    theirs = _get_json(cloud, f"{remote}/metadata.json")
    if theirs is None:
        raise CloudError("that cloud folder has no metadata.json, so its timeline cannot be lined "
                         "up with this recording / 云盘那份没有 metadata.json，没法和这段录音对齐时间")
    a, b = clock_seconds(mine.get("start_time")), clock_seconds(theirs.get("start_time"))
    if (normalize_date(theirs.get("date")) != normalize_date(mine.get("date"))
            or a is None or b is None):
        raise CloudError("that cloud meeting's date / start time does not line up with this "
                         "recording / 云盘那场会的日期或开始时间和这段录音对不上")
    shift = b - a
    segments = [replace(s, start=s.start + shift, end=s.end + shift, speaker=None)
                for s in _their_segments(cloud, remote)]
    if own:
        backup = tdir / f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir(parents=True)
        for p in own:
            p.replace(backup / p.name)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "source.json").write_text(json.dumps({
        "reused_from": remote, "uploaded_by": uploader, "their_start_time": theirs.get("start_time"),
        "shift_seconds": shift, "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    ai_input = build_ai_input(mine, segments)
    for seg in ai_input["transcript_segments"]:  # build_ai_input clamps negatives to 00:00:00
        seg["start"], seg["end"] = signed_clock(seg["start_seconds"]), signed_clock(seg["end_seconds"])
    (meeting_dir / "ai_input.json").write_text(
        json.dumps(ai_input, ensure_ascii=False, indent=2), encoding="utf-8")
    write_signed_transcript(segments, tdir / "transcript.md")  # last: postprocess's done-marker
    return {"segments": len(segments), "shift_seconds": shift, "from": remote}


def fetch_results(cloud: Cloud, remote: str, meeting_dir: Path) -> Path:
    """Copy a cloud meeting's results (text + slides, not audio) under meeting_dir/from_cloud/."""
    _require_complete(cloud, remote)
    target = meeting_dir / "from_cloud" / safe_name(remote.rstrip("/").rsplit("/", 1)[-1])
    names = _names(cloud.list(remote))
    for rel in ("summary.md", "interpretation.md", "ai_input.json", "metadata.json", UPLOAD_MARKER):
        if rel in names:
            cloud.download(f"{remote}/{rel}", target / rel)
    for sub in ("transcript", "slides"):
        if f"{sub}/" in names:
            for e in cloud.list(f"{remote}/{sub}"):
                if not e.is_dir and not e.name.startswith("_"):
                    cloud.download(e.path, target / sub / e.name)
    return target


def upload_items(meeting_dir: Path, notes_only: bool = False) -> list[tuple[Path, str]]:
    """(local file, path inside the cloud meeting folder) for what goes up, audio excluded."""
    keep = ("summary.md", "interpretation.md") if notes_only else _UPLOAD_FILES
    items = [(meeting_dir / rel, rel) for rel in keep if (meeting_dir / rel).is_file()]
    items += [(png, f"slides/{png.name}") for png in sorted((meeting_dir / "slides").glob("*.png"))]
    return items


def upload_meeting(cloud: Cloud, dest: str, meeting_dir: Path, *, uploader: str, login: str,
                   notes_only: bool = False, version: str = "",
                   log: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True),
                   ) -> dict[str, Any]:
    """Upload a meeting directory to `dest` (which the caller has chosen or claimed).

    Order: the marker with status "uploading" (whoever checks meanwhile sees an
    upload under way), metadata.json (makes the folder recognisable as this
    meeting), the rest, then the marker again as "complete". The marker lists
    every file's sha256: a rerun skips a file only when the size on the cloud
    and the recorded hash both match the local file, so an edited summary of
    the same length still goes up. `notes_only` uploads just this user's
    summary/interpretation (and the slides they point at), always in full.
    """
    from .remote import prepare_flac  # noqa: PLC0415

    items = upload_items(meeting_dir, notes_only)
    items.sort(key=lambda item: item[1] != "metadata.json")  # metadata first
    existing: dict[str, int] = {}
    previous: dict[str, Any] = {}
    if not notes_only:
        for sub in ("", "slides", "transcript", "audio"):
            for e in cloud.list_or_empty(f"{dest}/{sub}".rstrip("/")):
                if not e.is_dir:
                    existing[f"{sub}/{e.name}".lstrip("/")] = e.size
        old = _get_json(cloud, f"{dest}/{UPLOAD_MARKER}") or {}
        if old.get("login") == login and isinstance(old.get("files"), dict):
            previous = old["files"]
    cloud.mkdir(dest)
    marker: dict[str, Any] = {
        "uploaded_by": uploader, "login": login, "status": "uploading",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "meetingmind": version, "audio": "16 kHz mono FLAC", "files": dict(previous),
    }

    def write_marker() -> None:
        if not notes_only:
            cloud.put_bytes(f"{dest}/{UPLOAD_MARKER}", json.dumps(marker, ensure_ascii=False).encode())

    write_marker()
    made: set[str] = set()
    sent = skipped = 0

    def send(local: Path, rel: str) -> None:
        nonlocal sent, skipped
        digest = sha256_file(local)
        if existing.get(rel) == local.stat().st_size and previous.get(rel) == digest:
            skipped += 1
            return
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if parent and parent not in made:
            cloud.mkdir(f"{dest}/{parent}")
            made.add(parent)
        cloud.upload(local, f"{dest}/{rel}")
        marker["files"][rel] = digest
        sent += 1

    for local, rel in items:
        send(local, rel)
    write_marker()  # progress so far: a resume after this point skips all of it
    log(f"[cloud] {sent} file(s) up, {skipped} already there")
    if not notes_only:
        with tempfile.TemporaryDirectory(prefix="meetingmind_cloud_") as tmp:
            for wav_name, flac_name in _AUDIO:
                wav = meeting_dir / "audio" / wav_name
                if wav.is_file():
                    flac = Path(tmp) / flac_name
                    seconds = prepare_flac(wav, flac)
                    log(f"[cloud] audio {wav_name}: {seconds / 60:.0f} min → "
                        f"{flac.stat().st_size / 1048576:.1f} MB FLAC")
                    send(flac, f"audio/{flac_name}")
        marker.update(status="complete", finished_at=datetime.now().isoformat(timespec="seconds"))
        write_marker()
    return {"path": dest, "uploaded": sent, "skipped": skipped, "url": web_url(cloud.server, dest)}
