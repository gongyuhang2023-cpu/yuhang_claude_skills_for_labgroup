"""Test cloud — sign-in, lookup, transcript reuse and upload, against an in-memory WebDAV."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from meetingmind import cli, cloud
from meetingmind.cloud import Cloud, CloudError

SERVER = "https://cloud.example.org"
ROOT = "/lab/meetings"


class FakeDav:
    """Just enough of Nextcloud's WebDAV, login flow and OCS for these tests."""

    def __init__(self, user: str = "u1") -> None:
        self.user = user
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.dav = f"{SERVER}/remote.php/dav/files/{user}"
        self.uploads = f"{SERVER}/remote.php/dav/uploads/{user}"
        self.staged: dict[str, dict[str, bytes]] = {}

    # tree helpers
    def put(self, path: str, data: bytes | str) -> None:
        self.mkdirs(path.rsplit("/", 1)[0])
        self.files[path] = data.encode() if isinstance(data, str) else data

    def mkdirs(self, path: str) -> None:
        parts = [p for p in path.split("/") if p]
        for i in range(1, len(parts) + 1):
            self.dirs.add("/" + "/".join(parts[:i]))

    def _multistatus(self, path: str, depth: str) -> bytes:
        def response(p: str, is_dir: bool) -> str:
            href = urllib.parse.quote(f"/remote.php/dav/files/{self.user}{p}" + ("/" if is_dir else ""))
            size = 0 if is_dir else len(self.files[p])
            rtype = "<d:collection/>" if is_dir else ""
            return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                    f"<d:resourcetype>{rtype}</d:resourcetype><oc:size>{size}</oc:size>"
                    f"<d:getlastmodified>Fri, 23 Oct 2026 12:00:00 GMT</d:getlastmodified>"
                    f"</d:prop></d:propstat></d:response>")

        rows = [response(path, True)]
        if depth == "1":
            prefix = path.rstrip("/") + "/"
            rows += [response(d, True) for d in sorted(self.dirs)
                     if d.startswith(prefix) and "/" not in d[len(prefix):]]
            rows += [response(f, False) for f in sorted(self.files)
                     if f.startswith(prefix) and "/" not in f[len(prefix):]]
        return ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
                'xmlns:oc="http://owncloud.org/ns">' + "".join(rows) + "</d:multistatus>").encode()

    def __call__(self, method: str, url: str, *, data: bytes | None = None,
                 headers: dict[str, str] | None = None, auth: Any = None,
                 timeout: float = 0) -> tuple[int, bytes, dict[str, str]]:
        headers = headers or {}
        self.calls.append((method, url, headers))
        if url.startswith(self.uploads):
            return self._chunk(method, url, data, headers)
        if url.endswith("/ocs/v2.php/cloud/user?format=json"):
            body = {"ocs": {"data": {"id": self.user, "display-name": "Alice"}}}
            return 200, json.dumps(body).encode(), {}
        path = urllib.parse.unquote(url[len(self.dav):]).rstrip("/") or "/"
        if method == "PROPFIND":
            if path in self.dirs:
                return 207, self._multistatus(path, headers.get("Depth", "1")), {}
            if path in self.files:
                return 207, b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>', {}
            return 404, b"", {}
        if method == "GET":
            return (200, self.files[path], {}) if path in self.files else (404, b"", {})
        if method == "PUT":
            self.put(path, data or b"")
            return 201, b"", {}
        if method == "MKCOL":
            if path in self.dirs:
                return 405, b"", {}
            self.dirs.add(path)
            return 201, b"", {}
        if method == "DELETE":
            self.files = {k: v for k, v in self.files.items() if not k.startswith(path)}
            self.dirs = {d for d in self.dirs if not d.startswith(path)}
            return 204, b"", {}
        raise AssertionError(f"unexpected {method} {url}")

    def _chunk(self, method: str, url: str, data: bytes | None,
               headers: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
        staging, _, rest = url[len(self.uploads) + 1:].partition("/")
        if method == "MKCOL":
            self.staged[staging] = {}
            return 201, b"", {}
        if method == "PUT":
            self.staged[staging][rest] = data or b""
            return 201, b"", {}
        if method == "MOVE" and rest == ".file":
            dest = urllib.parse.unquote(headers["Destination"][len(self.dav):])
            chunks = self.staged.pop(staging)
            self.put(dest, b"".join(chunks[k] for k in sorted(chunks)))
            return 201, b"", {}
        raise AssertionError(f"unexpected chunk {method} {url}")

    def puts(self) -> list[str]:
        return [urllib.parse.unquote(u[len(self.dav):]) for m, u, _ in self.calls
                if m == "PUT" and u.startswith(self.dav)]


@pytest.fixture
def dav() -> FakeDav:
    return FakeDav()


@pytest.fixture
def client(dav: FakeDav) -> Cloud:
    return Cloud(SERVER, "u1", "alice@example.org", "app-password", transport=dav)


def _meta(start: str = "12:34:31", duration: int = 3340, **extra: Any) -> dict[str, Any]:
    return {"date": "2026-10-23", "start_time": start, "duration_seconds": duration,
            "recording_status": "complete", "slides": [], **extra}


def _meeting_dir(base: Path, meta: dict[str, Any]) -> Path:
    d = base / "2026-10-23-group-meeting"
    (d / "slides").mkdir(parents=True)
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


# --------------------------------------------------------------------- WebDAV and sign-in


def test_list_parses_multistatus_and_skips_the_folder_itself(dav: FakeDav, client: Cloud) -> None:
    dav.put(f"{ROOT}/m1/metadata.json", "{}")
    dav.mkdirs(f"{ROOT}/m1/slides")
    entries = {e.name: e for e in client.list(f"{ROOT}/m1")}
    assert set(entries) == {"metadata.json", "slides"}
    assert entries["slides"].is_dir and not entries["metadata.json"].is_dir
    assert entries["metadata.json"].size == 2
    assert entries["metadata.json"].path == f"{ROOT}/m1/metadata.json"


def test_login_flow_polls_until_the_user_approves() -> None:
    replies = [
        (200, json.dumps({"login": f"{SERVER}/login/v2/flow/x",
                          "poll": {"token": "t", "endpoint": f"{SERVER}/login/v2/poll"}}).encode(), {}),
        (404, b"", {}), (404, b"", {}),
        (200, json.dumps({"server": SERVER, "loginName": "alice", "appPassword": "pw"}).encode(), {}),
    ]
    shown: list[str] = []
    result = cloud.login_flow(SERVER, transport=lambda *a, **k: replies.pop(0),
                              show=shown.append, sleep=lambda _s: None)
    assert result == (SERVER, "alice", "pw")
    assert shown == [f"{SERVER}/login/v2/flow/x"]


def test_login_flow_gives_up_after_its_lifetime() -> None:
    start = (200, json.dumps({"login": "l", "poll": {"token": "t", "endpoint": "e"}}).encode(), {})
    replies = iter([start])

    def transport(*a: Any, **k: Any) -> tuple[int, bytes, dict[str, str]]:
        return next(replies, (404, b"", {}))

    with pytest.raises(CloudError, match="20"):
        cloud.login_flow(SERVER, transport=transport, sleep=lambda _s: None, timeout_s=6)


def test_expired_app_password_says_to_log_in_again(client: Cloud) -> None:
    client.transport = lambda *a, **k: (401, b"", {})
    with pytest.raises(CloudError, match="cloud login"):
        client.list(ROOT)


def test_big_files_go_through_chunked_upload(dav: FakeDav, client: Cloud, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloudflare refuses bodies over 100 MB: no single PUT may carry a big file."""
    monkeypatch.setattr(cloud, "CHUNK_THRESHOLD", 1000)
    monkeypatch.setattr(cloud, "CHUNK_SIZE", 400)
    blob = bytes(range(256)) * 10  # 2560 bytes → 7 chunks
    local = tmp_path / "audio.flac"
    local.write_bytes(blob)
    client.upload(local, f"{ROOT}/m/audio.flac")
    assert dav.files[f"{ROOT}/m/audio.flac"] == blob
    assert not dav.puts(), "nothing may go up as one plain PUT"
    assert [m for m, u, _ in dav.calls].count("PUT") == 7


# --------------------------------------------------------------------- finding the same meeting


@pytest.mark.parametrize(("a", "b", "share"), [
    ((0, 100), (0, 100), 1.0),
    ((0, 100), (50, 100), 0.5),
    ((0, 100), (20, 30), 1.0),        # one inside the other
    ((0, 100), (100, 50), 0.0),       # back to back
    ((0, 0), (0, 10), 0.0),
])
def test_overlap_share(a: tuple, b: tuple, share: float) -> None:
    assert cloud.overlap_share(a[0], a[1], b[0], b[1]) == pytest.approx(share)


def test_find_meetings(dav: FakeDav, client: Cloud) -> None:
    dav.put(f"{ROOT}/2026-10-23-lab-meeting/metadata.json",
            json.dumps(_meta("12:30:00", 3600, mic_captured=False)))
    dav.put(f"{ROOT}/2026-10-23-lab-meeting/{cloud.UPLOAD_MARKER}",
            json.dumps({"uploaded_by": "Bob", "login": "bob@x", "status": "complete"}))
    dav.put(f"{ROOT}/2026-10-23-lab-meeting/ai_input.json", "{}")
    dav.put(f"{ROOT}/2026-10-23-lab-meeting/summary.md", "s")
    dav.put(f"{ROOT}/20261023/2026-10-23-seminar/metadata.json", json.dumps(_meta("16:00:00", 1800)))
    dav.put(f"{ROOT}/2026-10-23-by-hand/slide_001.png", b"png")
    dav.put(f"{ROOT}/2026-10-22-yesterday/metadata.json", json.dumps(_meta()))
    dav.put(f"{ROOT}/no-date-here/notes.md", "n")

    found = cloud.find_meetings(client, ROOT, _meta("12:34:31", 3000))

    assert [m.name for m in found] == ["2026-10-23-lab-meeting", "2026-10-23-by-hand"]
    best = found[0]
    assert best.same_meeting and best.overlap == pytest.approx(1.0)
    assert (best.uploader, best.upload_status, best.self_voice) == ("Bob", "complete", False)
    assert best.has_transcript and "summary.md" in best.files
    assert found[1].overlap is None, "no metadata: listed for the user to judge"
    fetched = [urllib.parse.unquote(u) for m, u, _ in dav.calls if m == "GET"]
    assert not any("2026-10-22" in u for u in fetched), "other days are skipped by name"


def test_nested_meeting_is_compared_too(dav: FakeDav, client: Cloud) -> None:
    dav.put(f"{ROOT}/20261023/2026-10-23-seminar/metadata.json", json.dumps(_meta("16:00:00", 1800)))
    found = cloud.find_meetings(client, ROOT, _meta("16:05:00", 1500))
    assert [m.path for m in found] == [f"{ROOT}/20261023/2026-10-23-seminar"]
    assert found[0].same_meeting


def test_missing_meetings_folder_is_empty_not_an_error(client: Cloud) -> None:
    assert cloud.find_meetings(client, "/not/there", _meta()) == []


# --------------------------------------------------------------------- reusing a transcript


def test_signed_transcript_roundtrip(tmp_path: Path) -> None:
    from meetingmind.transcribe import Segment

    segs = [Segment(-300.0, -120.0, "before I started"), Segment(-120.0, 60.0, "overlap"),
            Segment(60.0, 240.0, "after")]
    path = tmp_path / "transcript.md"
    cloud.write_signed_transcript(segs, path)
    text = path.read_text(encoding="utf-8")
    assert "[-00:05:00] before I started" in text and "[00:01:00] after" in text
    back = cloud.parse_transcript_md(text)
    assert [(s.start, s.text) for s in back] == [(-300.0, "before I started"), (-120.0, "overlap"),
                                                  (60.0, "after")]
    assert back[0].end == -120.0


def test_reuse_shifts_their_transcript_onto_my_timeline(dav: FakeDav, client: Cloud,
                                                        tmp_path: Path) -> None:
    remote = f"{ROOT}/2026-10-23-lab-meeting"
    dav.put(f"{remote}/metadata.json", json.dumps(_meta("12:30:00", 3600)))
    dav.put(f"{remote}/ai_input.json", json.dumps({"transcript_segments": [
        {"start_seconds": 0, "end_seconds": 180, "text": "welcome", "speaker": "我"},
        {"start_seconds": 180, "end_seconds": 360, "text": "results", "speaker": None},
    ]}))
    mine = _meeting_dir(tmp_path, _meta("12:32:00", 3000))

    out = cloud.reuse_transcript(client, remote, mine, uploader="Bob")

    assert out == {"segments": 2, "shift_seconds": -120, "from": remote}
    ai = json.loads((mine / "ai_input.json").read_text(encoding="utf-8"))
    assert [(s["start_seconds"], s["speaker"]) for s in ai["transcript_segments"]] == [
        (-120.0, None), (60.0, None)], "their '我' is not this user"
    assert ai["meta"]["start_time"] == "12:32:00"
    md = (mine / "transcript" / "transcript.md").read_text(encoding="utf-8")
    assert "[-00:02:00] welcome" in md
    source = json.loads((mine / "transcript" / "source.json").read_text(encoding="utf-8"))
    assert (source["uploaded_by"], source["shift_seconds"]) == ("Bob", -120)


def test_reuse_falls_back_to_transcript_md(dav: FakeDav, client: Cloud, tmp_path: Path) -> None:
    remote = f"{ROOT}/old-upload"
    dav.put(f"{remote}/metadata.json", json.dumps(_meta("12:34:31", 600)))
    dav.put(f"{remote}/transcript/transcript.md", "# 会议转录\n\n[00:00:00] hello\n\n[00:03:00] bye\n\n")
    mine = _meeting_dir(tmp_path, _meta("12:34:31", 600))
    assert cloud.reuse_transcript(client, remote, mine)["segments"] == 2


def test_reuse_refuses_another_day(dav: FakeDav, client: Cloud, tmp_path: Path) -> None:
    dav.put(f"{ROOT}/x/metadata.json", json.dumps({**_meta(), "date": "2026-10-22"}))
    with pytest.raises(CloudError, match="line up"):
        cloud.reuse_transcript(client, f"{ROOT}/x", _meeting_dir(tmp_path, _meta()))


def _reusable(dav: FakeDav, remote: str, **ai: Any) -> None:
    dav.put(f"{remote}/metadata.json", json.dumps(_meta("12:30:00", 3600)))
    dav.put(f"{remote}/ai_input.json", json.dumps(ai or {"transcript_segments": [
        {"start_seconds": 0, "end_seconds": 180, "text": "welcome"}]}))


def test_reuse_keeps_an_existing_transcript_unless_forced(dav: FakeDav, client: Cloud,
                                                          tmp_path: Path) -> None:
    remote = f"{ROOT}/m"
    _reusable(dav, remote)
    mine = _meeting_dir(tmp_path, _meta("12:30:00", 3600))
    (mine / "transcript").mkdir()
    (mine / "transcript" / "transcript.md").write_text("my own", encoding="utf-8")
    with pytest.raises(CloudError, match="--force"):
        cloud.reuse_transcript(client, remote, mine)
    assert (mine / "transcript" / "transcript.md").read_text(encoding="utf-8") == "my own"

    cloud.reuse_transcript(client, remote, mine, force=True)
    backups = list((mine / "transcript").glob("backup-*/transcript.md"))
    assert [b.read_text(encoding="utf-8") for b in backups] == ["my own"]


def test_empty_ai_input_falls_back_to_the_markdown(dav: FakeDav, client: Cloud, tmp_path: Path) -> None:
    remote = f"{ROOT}/m"
    _reusable(dav, remote, note="no segments here")
    dav.put(f"{remote}/transcript/transcript.md",
            "# 会议转录\n\n[00:00:00] **我**: hello\n\n[00:03:00] **对方**: bye\n\n")
    mine = _meeting_dir(tmp_path, _meta("12:30:00", 3600))
    assert cloud.reuse_transcript(client, remote, mine)["segments"] == 2
    ai = json.loads((mine / "ai_input.json").read_text(encoding="utf-8"))
    assert [s["text"] for s in ai["transcript_segments"]] == ["hello", "bye"], "labels stripped"


def test_no_transcript_text_anywhere_is_an_error_not_an_empty_success(
        dav: FakeDav, client: Cloud, tmp_path: Path) -> None:
    remote = f"{ROOT}/m"
    _reusable(dav, remote, transcript_segments=[])
    mine = _meeting_dir(tmp_path, _meta("12:30:00", 3600))
    with pytest.raises(CloudError, match="no transcript"):
        cloud.reuse_transcript(client, remote, mine)
    assert not (mine / "transcript" / "transcript.md").exists(), "no empty done-marker"


def test_reused_times_before_my_start_keep_their_sign(dav: FakeDav, client: Cloud,
                                                      tmp_path: Path) -> None:
    remote = f"{ROOT}/m"
    _reusable(dav, remote)
    mine = _meeting_dir(tmp_path, _meta("12:32:00", 3000))
    cloud.reuse_transcript(client, remote, mine)
    seg = json.loads((mine / "ai_input.json").read_text(encoding="utf-8"))["transcript_segments"][0]
    assert (seg["start_seconds"], seg["start"], seg["end"]) == (-120.0, "-00:02:00", "00:01:00")


def test_reuse_needs_metadata_to_line_up(dav: FakeDav, client: Cloud, tmp_path: Path) -> None:
    dav.put(f"{ROOT}/by-hand/transcript/transcript.md", "[00:00:00] hi\n")
    with pytest.raises(CloudError, match="metadata"):
        cloud.reuse_transcript(client, f"{ROOT}/by-hand", _meeting_dir(tmp_path, _meta()))


def test_an_unfinished_upload_is_not_reused(dav: FakeDav, client: Cloud, tmp_path: Path) -> None:
    remote = f"{ROOT}/m"
    _reusable(dav, remote)
    dav.put(f"{remote}/{cloud.UPLOAD_MARKER}", json.dumps({"status": "uploading", "login": "bob"}))
    mine = _meeting_dir(tmp_path, _meta("12:30:00", 3600))
    with pytest.raises(CloudError, match="still being uploaded"):
        cloud.reuse_transcript(client, remote, mine)
    with pytest.raises(CloudError, match="still being uploaded"):
        cloud.fetch_results(client, remote, mine)
    found = cloud.find_meetings(client, ROOT, _meta("12:30:00", 3600))
    assert found and found[0].as_dict()["upload_complete"] is False


def test_metadata_dates_in_either_spelling_match(dav: FakeDav, client: Cloud) -> None:
    dav.put(f"{ROOT}/2026-10-23-a/metadata.json", json.dumps({**_meta(), "date": "20261023"}))
    assert [m.name for m in cloud.find_meetings(client, ROOT, _meta())] == ["2026-10-23-a"]


@pytest.mark.parametrize("bad", ["null", "[1, 2]", '{"date": "2026-10-23", "duration_seconds": {}}',
                                 "not json at all"])
def test_one_broken_metadata_does_not_break_the_lookup(dav: FakeDav, client: Cloud, bad: str) -> None:
    dav.put(f"{ROOT}/2026-10-23-broken/metadata.json", bad)
    dav.put(f"{ROOT}/2026-10-23-broken/{cloud.UPLOAD_MARKER}", "[]")
    dav.put(f"{ROOT}/2026-10-23-good/metadata.json", json.dumps(_meta()))
    names = [m.name for m in cloud.find_meetings(client, ROOT, _meta())]
    assert "2026-10-23-good" in names


# --------------------------------------------------------------------- uploading


def _finished_meeting(tmp_path: Path) -> Path:
    d = _meeting_dir(tmp_path, _meta())
    for rel, text in (("ai_input.json", "{}"), ("summary.md", "s"), ("interpretation.md", "i"),
                      ("transcript/transcript.md", "t"), ("transcript/_partial.md", "p"),
                      ("transcript/spark_job.json", "{}"), ("interpretation_batch_01.md", "b")):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    (d / "slides" / "slide_001.png").write_bytes(b"png")
    (d / "audio").mkdir()
    sf.write(d / "audio" / "recording.wav", np.zeros((48000, 2), dtype="float32"), 48000,
             subtype="PCM_16")
    return d


def test_upload_sends_the_meeting_with_marker_first_and_last(dav: FakeDav, client: Cloud,
                                                             tmp_path: Path) -> None:
    d = _finished_meeting(tmp_path)
    dest = f"{ROOT}/2026-10-23-group-meeting"
    out = cloud.upload_meeting(client, dest, d, uploader="Alice", login="alice@example.org",
                               log=lambda _m: None)

    sent = dav.puts()
    assert sent[0] == f"{dest}/{cloud.UPLOAD_MARKER}" and sent[-1] == f"{dest}/{cloud.UPLOAD_MARKER}"
    assert sent[1] == f"{dest}/metadata.json", "metadata right after the marker: recognisable early"
    marker = json.loads(dav.files[f"{dest}/{cloud.UPLOAD_MARKER}"])
    assert marker["status"] == "complete"
    assert set(marker["files"]) >= {"metadata.json", "summary.md", "audio/recording.flac"}
    names = {p[len(dest) + 1:] for p in dav.files if p.startswith(dest)}
    assert names == {cloud.UPLOAD_MARKER, "metadata.json", "ai_input.json", "summary.md",
                     "interpretation.md", "transcript/transcript.md", "slides/slide_001.png",
                     "audio/recording.flac"}, "no partials, job state, batch files or WAV"
    assert dav.files[f"{dest}/audio/recording.flac"][:4] == b"fLaC"
    assert out["uploaded"] == 7 and out["url"].endswith(urllib.parse.quote(dest))


def test_upload_again_skips_what_is_already_there(dav: FakeDav, client: Cloud, tmp_path: Path) -> None:
    d = _finished_meeting(tmp_path)
    dest = f"{ROOT}/2026-10-23-group-meeting"
    cloud.upload_meeting(client, dest, d, uploader="Alice", login="a", log=lambda _m: None)
    again = cloud.upload_meeting(client, dest, d, uploader="Alice", login="a", log=lambda _m: None)
    assert (again["uploaded"], again["skipped"]) == (0, 7)


def test_same_size_edit_is_still_uploaded(dav: FakeDav, client: Cloud, tmp_path: Path) -> None:
    """Size alone cannot tell an edited summary of the same length from the old one."""
    d = _finished_meeting(tmp_path)
    dest = f"{ROOT}/2026-10-23-group-meeting"
    cloud.upload_meeting(client, dest, d, uploader="Alice", login="a", log=lambda _m: None)
    (d / "summary.md").write_text("S", encoding="utf-8")  # same length, new content
    again = cloud.upload_meeting(client, dest, d, uploader="Alice", login="a", log=lambda _m: None)
    assert again["uploaded"] == 1
    assert dav.files[f"{dest}/summary.md"] == b"S"


def test_someone_elses_marker_hashes_are_not_trusted(dav: FakeDav, client: Cloud,
                                                     tmp_path: Path) -> None:
    d = _finished_meeting(tmp_path)
    dest = f"{ROOT}/2026-10-23-group-meeting"
    cloud.upload_meeting(client, dest, d, uploader="Bob", login="bob", log=lambda _m: None)
    again = cloud.upload_meeting(client, dest, d, uploader="Alice", login="alice", log=lambda _m: None)
    assert again["skipped"] == 0


def test_notes_only_go_into_a_subfolder_without_a_marker(dav: FakeDav, client: Cloud,
                                                         tmp_path: Path) -> None:
    d = _finished_meeting(tmp_path)
    dest = f"{ROOT}/theirs/Alice"
    cloud.upload_meeting(client, dest, d, uploader="Alice", login="a", notes_only=True,
                         log=lambda _m: None)
    names = {p[len(dest) + 1:] for p in dav.files if p.startswith(dest)}
    assert names == {"summary.md", "interpretation.md", "slides/slide_001.png"}


# --------------------------------------------------------------------- CLI glue


def test_login_asks_the_lab_server_only_when_nothing_is_known(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The cloud's address is not in the published skill: it comes from the server, once."""
    from meetingmind import config, remote

    asked: list[tuple] = []
    monkeypatch.setattr(remote.SparkClient, "call", lambda self, *a, **k: asked.append(a) or
                        {"cloud_url": "https://cloud.example.org", "cloud_folder": "/lab/meetings"})
    args = cli.build_parser().parse_args(["cloud", "login"])
    assert cli._login_target(config.Config(), args) == ("https://cloud.example.org", "/lab/meetings")
    assert asked == [("lab",)]

    saved = config.Config(cloud_url="https://saved.example.org", cloud_folder="/saved")
    assert cli._login_target(saved, args) == ("https://saved.example.org", "/saved")
    assert len(asked) == 1, "nothing asked when the config already knows"

    override = cli.build_parser().parse_args(["cloud", "login", "--folder", "/other"])
    assert cli._login_target(saved, override) == ("https://saved.example.org", "/other")


def test_login_without_server_says_what_to_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from meetingmind import config, remote

    def unreachable(self: Any, *a: Any, **k: Any) -> dict:
        raise remote.RemoteError("Cannot reach spark over ssh")

    monkeypatch.setattr(remote.SparkClient, "call", unreachable)
    with pytest.raises(RuntimeError, match="--url and --folder"):
        cli._login_target(config.Config(), cli.build_parser().parse_args(["cloud", "login"]))


def test_git_bash_path_rewrite_is_undone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEPATH", r"C:\Program Files\Git")
    assert cli._unmangle("C:/Program Files/Git/lab/meetings") == "/lab/meetings"
    assert cli._unmangle("/lab/meetings") == "/lab/meetings"
    assert cli._unmangle("2026-10-23-x") == "2026-10-23-x"
    monkeypatch.setenv("EXEPATH", "")
    assert cli._unmangle(r"D:\Tools\Git\lab") == "/lab"


def test_upload_command_refuses_a_duplicate_and_resumes_own(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from meetingmind import config

    d = _finished_meeting(tmp_path)
    cfg = config.Config(cloud_url=SERVER, cloud_login="alice@example.org", cloud_user="u1",
                        cloud_folder=ROOT)
    theirs = cloud.RemoteMeeting(path=f"{ROOT}/b", name="b", date="2026-10-23", overlap=0.9,
                                 uploader="Bob", uploader_login="bob@x")
    sent: list[tuple[str, bool]] = []
    monkeypatch.setattr(cloud, "display_name", lambda *a, **k: "Alice")
    monkeypatch.setattr(cloud, "upload_meeting",
                        lambda c, dest, md, **k: sent.append((dest, k["notes_only"])) or {"url": "u"})
    args = cli.build_parser().parse_args(["cloud", "upload", str(d), "--json"])

    client = Cloud(SERVER, "u1", "alice@example.org", "pw", transport=FakeDav())
    monkeypatch.setattr(cli, "_cloud_find", lambda cfg, md: (client, [theirs]))
    assert cli._cloud_upload_cmd(cfg, args) == 3 and not sent
    assert json.loads(capsys.readouterr().out)["status"] == "duplicate"

    args.mode = "notes"
    assert cli._cloud_upload_cmd(cfg, args) == 0
    notes_dir = f"{ROOT}/b/{cloud.notes_folder('Alice', 'alice@example.org')}"
    assert sent[-1] == (notes_dir, True)
    assert not (d / cloud.LOCAL_RECORD).exists(), "notes are not this meeting's own upload"

    own = cloud.RemoteMeeting(path=f"{ROOT}/mine", name="mine", date="2026-10-23", overlap=1.0,
                              uploader_login="alice@example.org")
    monkeypatch.setattr(cli, "_cloud_find", lambda cfg, md: (client, [own, theirs]))
    args.mode = None
    assert cli._cloud_upload_cmd(cfg, args) == 0
    assert sent[-1] == (f"{ROOT}/mine", False), "own earlier upload is finished, not flagged"

    args.mode = "notes"  # explicit choice beats resuming: never the whole meeting
    assert cli._cloud_upload_cmd(cfg, args) == 0
    assert sent[-1] == (notes_dir, True)


def test_new_upload_claims_a_free_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two people uploading at once must not share a folder: the name is taken with MKCOL."""
    from meetingmind import config

    d = _finished_meeting(tmp_path)
    dav = FakeDav()
    dav.mkdirs(f"{ROOT}/2026-10-23-group-meeting")  # taken by someone a moment ago
    client = Cloud(SERVER, "u1", "alice@example.org", "pw", transport=dav)
    cfg = config.Config(cloud_url=SERVER, cloud_login="alice@example.org", cloud_user="u1",
                        cloud_folder=ROOT)
    sent: list[str] = []
    monkeypatch.setattr(cli, "_cloud_find", lambda cfg, md: (client, []))
    monkeypatch.setattr(cloud, "upload_meeting", lambda c, dest, md, **k: sent.append(dest) or {"url": "u"})
    args = cli.build_parser().parse_args(["cloud", "upload", str(d)])
    assert cli._cloud_upload_cmd(cfg, args) == 0
    assert sent == [f"{ROOT}/2026-10-23-group-meeting-2"]
    record = json.loads((d / cloud.LOCAL_RECORD).read_text(encoding="utf-8"))
    assert (record["dest"], record["status"]) == (sent[0], "complete")


def test_interrupted_upload_resumes_into_the_same_folder(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopped before metadata.json went up, the cloud copy is unrecognisable — the local record is not."""
    from meetingmind import config

    d = _finished_meeting(tmp_path)
    dav = FakeDav()
    dav.mkdirs(f"{ROOT}/2026-10-23-group-meeting-2")
    (d / cloud.LOCAL_RECORD).write_text(json.dumps({
        "dest": f"{ROOT}/2026-10-23-group-meeting-2", "login": "alice@example.org",
        "status": "uploading"}), encoding="utf-8")
    client = Cloud(SERVER, "u1", "alice@example.org", "pw", transport=dav)
    cfg = config.Config(cloud_url=SERVER, cloud_login="alice@example.org", cloud_user="u1",
                        cloud_folder=ROOT)
    sent: list[str] = []
    theirs = cloud.RemoteMeeting(path=f"{ROOT}/b", name="b", date="2026-10-23", overlap=0.9,
                                 uploader_login="bob@x")
    monkeypatch.setattr(cli, "_cloud_find", lambda cfg, md: (client, [theirs]))
    monkeypatch.setattr(cloud, "upload_meeting", lambda c, dest, md, **k: sent.append(dest) or {"url": "u"})
    args = cli.build_parser().parse_args(["cloud", "upload", str(d)])
    assert cli._cloud_upload_cmd(cfg, args) == 0, "own unfinished upload wins over the duplicate check"
    assert sent == [f"{ROOT}/2026-10-23-group-meeting-2"]


@pytest.mark.parametrize("name", ["..", ".", "...", "/", ""])
def test_safe_name_never_yields_a_path_step(name: str) -> None:
    assert cloud.safe_name(name) not in ("", ".", "..")


def test_notes_folders_of_namesakes_differ() -> None:
    assert cloud.notes_folder("Wang Lei", "wang.lei@a") != cloud.notes_folder("Wang Lei", "wang.lei@b")
