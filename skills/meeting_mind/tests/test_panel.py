"""Test panel — what goes up to the lab panel's group-meeting page, and how the server's answer is read."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from meetingmind import cli, panel
from meetingmind.remote import RemoteError

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f"
       b"\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def make_meeting(root: Path, name: str = "2026-11-09-group-meeting", topic: str = "group-meeting",
                 metadata: bool = True) -> Path:
    d = root / name
    (d / "slides").mkdir(parents=True)
    (d / "transcript").mkdir()
    (d / "audio").mkdir()
    for i in (1, 2, 3):
        (d / "slides" / f"slide_00{i}.png").write_bytes(PNG)
    (d / "slides" / "notes.txt").write_text("not an image")
    (d / "transcript" / "transcript.md").write_text("secret words")
    (d / "audio" / "recording.wav").write_bytes(b"RIFF")
    (root / "outside.png").write_bytes(PNG)
    (d / "summary.md").write_text(
        "# 组会阅读稿 — 测试\n\n![a](slides/slide_002.png)\n![remote](https://x.org/y.png)\n"
        "![abs](/etc/z.png)\n![up](../outside.png)\n![gone](slides/missing.png)\n![txt](slides/notes.txt)\n",
        encoding="utf-8")
    (d / "interpretation.md").write_text(
        "## Slide 1\n![Slide 1](slides/slide_001.png)\n## Slide 2\n![Slide 2](<slides/slide_002.png> \"t\")\n",
        encoding="utf-8")
    if metadata:
        (d / "metadata.json").write_text(json.dumps(
            {"date": "20261109", "start_time": "13:58:22", "duration_seconds": 2970.4, "topic": topic}))
    return d


def tar_names(blob: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        return {m.name: m for m in tf.getmembers()}


# ------------------------------------------------------------------ what goes up
def test_referenced_images_only_screenshots_inside_the_meeting(tmp_path: Path) -> None:
    d = make_meeting(tmp_path)
    assert list(panel.referenced_images(d)) == ["slides/slide_002.png", "slides/slide_001.png"]


def test_upload_holds_the_notes_and_their_screenshots_and_nothing_else(tmp_path: Path) -> None:
    d = make_meeting(tmp_path)
    info = panel.meeting_info(d, panel.read_metadata(d))
    buf = io.BytesIO()
    sent = panel.build_upload(d, info, buf)
    members = tar_names(buf.getvalue())
    assert set(members) == {"meeting.json", "summary.md", "interpretation.md",
                            "slides/slide_001.png", "slides/slide_002.png"}
    assert sent["docs"] == ["summary.md", "interpretation.md"] and sent["images"] == 2
    assert all(m.uname == "" and m.uid == 0 and m.isfile() for m in members.values())
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as tf:
        assert json.loads(tf.extractfile("meeting.json").read()) == {
            "date": "2026-11-09", "start_time": "13:58:22", "duration_seconds": 2970.4, "topic": "group-meeting"}


def test_upload_needs_the_reading_notes(tmp_path: Path) -> None:
    d = make_meeting(tmp_path)
    (d / "summary.md").unlink()
    with pytest.raises(RuntimeError, match="summary.md"):
        panel.build_upload(d, panel.meeting_info(d, {}), io.BytesIO())


def test_meeting_info_falls_back_to_the_folder_name(tmp_path: Path) -> None:
    d = make_meeting(tmp_path, name="2026-10-19-group-meeting-p3", metadata=False)
    assert panel.meeting_info(d, panel.read_metadata(d)) == {
        "date": "2026-10-19", "start_time": None, "duration_seconds": None, "topic": ""}
    with pytest.raises(RuntimeError, match="date"):
        panel.meeting_info(tmp_path / "no-date", {})
    assert panel.meeting_info(d, {"date": "2026-10-19", "start_time": "2pm"})["start_time"] is None


@pytest.mark.parametrize(("topic", "name", "expected"), [
    ("本周组会", "2026-11-02-x", True), ("group-meeting", "x", True), ("Lab Meeting", "x", True),
    ("weekly-sync", "2026-11-09-group_meeting", True), ("weekly-sync", "2026-11-06-师兄", False),
    ("journal club", "2026-11-06-jc", False),
])
def test_group_meeting_from_topic_or_folder(topic: str, name: str, expected: bool) -> None:
    assert panel.looks_like_group_meeting(Path(name), {"topic": topic}) is expected


# ------------------------------------------------------------------ talking to spark
class Runner:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.result = (stdout, returncode, stderr)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
        self.calls.append((argv, kw))
        out, code, err = self.result
        return subprocess.CompletedProcess(argv, code, out, err)


def test_argv_direct_and_through_a_hop() -> None:
    direct = panel.PanelClient("spark").argv("check", "--date", "2026-11-09")
    assert direct[0] == "ssh" and direct[-2:] == ["spark", f"{panel.PANEL_COMMAND} check --date 2026-11-09"]
    hop = panel.PanelClient("spark", via="pc").argv("check", "--date", "2026-11-09")
    assert hop[:1] == ["ssh"] and hop[hop.index("pc") + 1] == "ssh" and hop[-1] == direct[-1]


def test_status_line_counts_even_when_the_hop_loses_the_exit_code() -> None:
    run = Runner('noise\n{"status": "duplicate", "matches": [{"id": "2026-11-09-abcdef"}]}\n', returncode=1)
    data = panel.PanelClient("spark", runner=run).call("publish")
    assert data["status"] == "duplicate"


@pytest.mark.parametrize(("stdout", "code", "stderr", "match"), [
    ('{"status": "refused", "message": "summary.md is missing"}', 4, "", "summary.md is missing"),
    ("", 127, "bash: /usr/local/bin/spark-meeting-publish: No such file or directory", "not set up"),
    ("", 1, "bash: line 1: /usr/local/bin/spark-meeting-publish: No such file or directory", "not set up"),
    ("", 255, "ssh: connect to host spark port 22: Connection timed out", "Cannot reach"),
])
def test_errors_are_explained(stdout: str, code: int, stderr: str, match: str) -> None:
    with pytest.raises(RemoteError, match=match):
        panel.PanelClient("spark", runner=Runner(stdout, code, stderr)).call("check")


def test_check_sends_times_only_when_both_are_known(tmp_path: Path) -> None:
    run = Runner('{"status": "absent", "matches": []}')
    client = panel.PanelClient("spark", runner=run)
    panel.check(client, {"date": "2026-11-09", "start_time": "13:58:22", "duration_seconds": 2970.4})
    panel.check(client, {"date": "2026-11-09", "start_time": "13:58:22", "duration_seconds": None})
    assert run.calls[0][0][-1].endswith("check --date 2026-11-09 --start 13:58:22 --duration 2970")
    assert run.calls[1][0][-1].endswith("check --date 2026-11-09")


def test_publish_streams_the_tar_on_stdin(tmp_path: Path) -> None:
    d = make_meeting(tmp_path)
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
        seen["argv"], seen["tar"] = argv, kw["stdin"].read()
        return subprocess.CompletedProcess(argv, 0, '{"status": "published", "id": "2026-11-09-abcdef"}', "")

    out = panel.publish(panel.PanelClient("spark", runner=run), d, panel.meeting_info(d, panel.read_metadata(d)),
                        update="2026-11-09-abcdef")
    assert out["status"] == "published" and out["sent"]["images"] == 2
    assert seen["argv"][-1].endswith("publish --update 2026-11-09-abcdef")
    assert "slides/slide_001.png" in tar_names(seen["tar"])


# ------------------------------------------------------------------ the command
@pytest.fixture
def no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEETINGMIND_CONFIG", str(tmp_path / "none.json"))


def fake_client(monkeypatch: pytest.MonkeyPatch, stdout: str) -> Runner:
    run = Runner(stdout)
    real = panel.PanelClient
    monkeypatch.setattr(panel, "PanelClient", lambda host, via=None: real(host, via=via, runner=run))
    return run


def test_cli_refuses_a_meeting_that_does_not_say_group_meeting(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, no_config: None) -> None:
    d = make_meeting(tmp_path, name="2026-11-06-师兄", topic="师兄")
    run = fake_client(monkeypatch, '{"status": "published"}')
    assert cli.main(["panel", "publish", str(d), "--json"]) == 4
    assert json.loads(capsys.readouterr().out)["status"] == "not_group_meeting" and not run.calls
    assert cli.main(["panel", "publish", str(d), "--json", "--group-meeting"]) == 0 and run.calls


def test_cli_duplicate_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture, no_config: None) -> None:
    d = make_meeting(tmp_path)
    fake_client(monkeypatch, '{"status": "duplicate", "matches": [{"id": "2026-11-09-abcdef", '
                             '"uploader": "u2", "mine": false, "same_meeting": true}]}')
    assert cli.main(["panel", "publish", str(d), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["matches"][0]["uploader"] == "u2"


def test_cli_check_reports_whether_it_is_a_group_meeting(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, no_config: None) -> None:
    d = make_meeting(tmp_path)
    fake_client(monkeypatch, '{"status": "absent", "matches": []}')
    assert cli.main(["panel", "check", str(d), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"status": "absent", "matches": [], "group_meeting": True, "has_summary": True}
