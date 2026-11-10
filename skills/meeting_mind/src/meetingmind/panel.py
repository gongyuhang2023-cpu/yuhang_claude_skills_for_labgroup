"""The lab panel's 「组会」 page: put a group meeting's reading notes where the whole lab reads them.

Whoever processes a group meeting first publishes it. The server (`spark-meeting-publish` on
spark, run over ssh as you) recognises the same meeting the way the cloud lookup does — same day,
recording times overlapping by at least half of the shorter one — so everyone after that is told
it is already there and skips it. The page shows who put it up.

Only the reading notes travel: summary.md, interpretation.md and the screenshots they show. Never
the transcript, the audio, or anything else in the meeting folder. The server renders the markdown
and shrinks the screenshots itself.

The server always answers with one JSON line carrying a `status`; that, not ssh's exit code, is
what counts here, because an ssh hop through another machine (`--via`) may not pass exit codes on.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .cloud import name_date, normalize_date
from .remote import SSH_OPTIONS, RemoteError, _last_json

#: Installed on spark by the admins (absolute: a non-interactive ssh has a bare PATH).
PANEL_COMMAND = "/usr/local/bin/spark-meeting-publish"
#: What goes up, relative to the meeting directory, besides the screenshots they show.
DOCS = ("summary.md", "interpretation.md")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
#: How a group meeting's topic or folder name usually reads. Anything else needs the user's word.
GROUP_MEETING = re.compile(r"组会|group[\s_-]*meeting|lab[\s_-]*meeting", re.IGNORECASE)
_IMAGE_REF = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HMS = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
#: Answers that mean "nothing was written, ask the user" rather than an error.
NOT_PUBLISHED = ("duplicate", "maybe", "not_group_meeting")


def read_metadata(meeting_dir: Path) -> dict[str, Any]:
    """metadata.json if the folder has one (a delivered copy of the notes may not)."""
    try:
        data = json.loads((meeting_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def meeting_info(meeting_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Date and recording span: what the server matches meetings by."""
    date = normalize_date(meta.get("date")) or name_date(meeting_dir.name)
    if not date:
        raise RuntimeError("cannot tell this meeting's date: no metadata.json and no date in the "
                           "folder name / 看不出这场会的日期（没有 metadata.json，文件夹名里也没有日期）")
    start = meta.get("start_time")
    try:
        duration = float(meta.get("duration_seconds") or 0) or None
    except (TypeError, ValueError):
        duration = None
    return {"date": date, "start_time": start if isinstance(start, str) and _HMS.match(start) else None,
            "duration_seconds": duration, "topic": str(meta.get("topic") or "")}


def looks_like_group_meeting(meeting_dir: Path, meta: dict[str, Any]) -> bool:
    return bool(GROUP_MEETING.search(f"{meta.get('topic') or ''} {meeting_dir.name}"))


def referenced_images(meeting_dir: Path) -> dict[str, Path]:
    """Screenshots the notes show, inside the meeting directory: {path as the notes write it: file}.

    The key is also the file's name in the upload, so the server finds it where the markdown points.
    """
    root = meeting_dir.resolve()
    found: dict[str, Path] = {}
    for name in DOCS:
        md = meeting_dir / name
        if not md.is_file():
            continue
        for src in _IMAGE_REF.findall(md.read_text(encoding="utf-8", errors="replace")):
            rel = Path(os.path.normpath(src)).as_posix()
            if _SCHEME.match(src) or src.startswith(("/", "\\")) or rel.startswith("../") or rel in found:
                continue
            path = (meeting_dir / rel).resolve()
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file() and root in path.parents:
                found[rel] = path
    return found


def _anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
    # tar headers carry the local user name and ids; the server has no use for them
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o644
    return info


def build_upload(meeting_dir: Path, info: dict[str, Any], out: IO[bytes]) -> dict[str, Any]:
    """Write the tar the server reads into `out`; return what went in."""
    if not (meeting_dir / "summary.md").is_file():
        raise RuntimeError("this meeting has no summary.md yet — finish Phase 4 first"
                           " / 这场会还没有会后阅读稿（summary.md），先完成解读和总结")
    docs = [name for name in DOCS if (meeting_dir / name).is_file()]
    images = referenced_images(meeting_dir)
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT, dereference=True) as tf:
        blob = json.dumps(info, ensure_ascii=False).encode("utf-8")
        entry = _anonymous(tarfile.TarInfo("meeting.json"))
        entry.size = len(blob)
        tf.addfile(entry, fileobj=io.BytesIO(blob))
        for name in docs:
            tf.add(str(meeting_dir / name), arcname=name, filter=_anonymous)
        for rel, path in images.items():
            tf.add(str(path), arcname=rel, filter=_anonymous)
    return {"docs": docs, "images": len(images), "bytes": out.tell()}


@dataclass
class PanelClient:
    """`spark-meeting-publish` on spark over ssh, optionally through one hop (`via`)."""

    host: str
    via: str | None = None
    command: str = PANEL_COMMAND
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def argv(self, *args: str) -> list[str]:
        remote = " ".join(shlex.quote(a) for a in (self.command, *args))
        argv = ["ssh", *SSH_OPTIONS, self.host, remote]
        return ["ssh", *SSH_OPTIONS, self.via, *argv] if self.via else argv

    def call(self, *args: str, stdin: IO[bytes] | None = None, timeout: float = 60) -> dict[str, Any]:
        where = f"{self.host} (via {self.via})" if self.via else self.host
        try:
            cp = self.runner(self.argv(*args), stdin=stdin, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout)
        except FileNotFoundError as exc:
            raise RemoteError("'ssh' is not installed on this machine"
                              " / 本机找不到 ssh 命令（需要 OpenSSH 客户端）") from exc
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(f"{where} did not answer within {timeout:.0f} s"
                              f" / {where} 在 {timeout:.0f} 秒内没有响应") from exc
        data = _last_json(cp.stdout)
        if data.get("status"):
            if data["status"] in ("error", "refused"):
                raise RemoteError(f"spark: {data.get('message') or data['status']}", data)
            return data
        tail = cp.stderr.strip()[-300:]
        if cp.returncode == 127 or (self.command in tail and ("No such file" in tail or "not found" in tail)):
            raise RemoteError(f"the panel upload is not set up on {self.host} ({self.command} is "
                              "missing); ask an admin / spark 上还没装面板上传，请联系管理员",
                              {"error": "not_deployed"})
        raise RemoteError(f"Cannot reach {where} over ssh: {tail or f'exit {cp.returncode}'}\n"
                          f"  Check that Tailscale is connected and `ssh {self.host}` logs in without "
                          f"a password. / 连不上 {self.host}：检查 Tailscale 是否在线，以及能否免密 ssh 登录。")


def check(client: PanelClient, info: dict[str, Any]) -> dict[str, Any]:
    args = ["check", "--date", info["date"]]
    if info["start_time"] and info["duration_seconds"]:
        args += ["--start", info["start_time"], "--duration", f"{info['duration_seconds']:.0f}"]
    return client.call(*args, timeout=60)


def publish(client: PanelClient, meeting_dir: Path, info: dict[str, Any], *,
            new: bool = False, update: str | None = None) -> dict[str, Any]:
    args = ["publish"] + (["--new"] if new else []) + (["--update", update] if update else [])
    with tempfile.TemporaryFile() as tar:
        sent = build_upload(meeting_dir, info, tar)
        tar.seek(0)
        # a long meeting is ~30-100 MB of screenshots; the server converts them after upload
        result = client.call(*args, stdin=tar, timeout=1800)
    return {**result, "sent": sent}
