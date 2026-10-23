"""Test I-recovery — classify honestly, repair minimally, never make things worse.

A recovery tool is the last thing standing between a damaged recording and no
recording, so the bar is not "usually works": it must refuse anything it does
not understand, and it must not touch a file that is already fine.
"""

from __future__ import annotations

import json
import os
import struct
import time
import wave
from pathlib import Path

import pytest

from meetingmind.recovery import (
    inspect_wav,
    rebuild_slides,
    recover_incomplete,
    repair_wav,
)
from meetingmind.wavsink import WavSink
from tests.helpers import make_float32_samples, to_pcm_bytes


def _make_wav(path: Path, frames: int = 4_800, channels: int = 2) -> Path:
    sink = WavSink(path, 48000, channels)
    sink.start()
    sink.feed(to_pcm_bytes(make_float32_samples(frames * channels)))
    sink.finish()
    return path


def _age(path: Path, seconds: float = 60.0) -> None:
    """Backdate mtime so the quiescence guard does not treat it as live."""
    past = time.time() - seconds
    os.utime(path, (past, past))


# ------------------------------------------------------------------- inspect


def test_healthy_file_is_ok_and_untouched(wav_path: Path) -> None:
    _make_wav(wav_path)
    before = wav_path.read_bytes()

    status = inspect_wav(wav_path)
    assert status.status == "ok"
    assert status.data_actual == status.data_declared
    assert status.duration_seconds == pytest.approx(0.1)

    # repair_wav on a healthy file must be a no-op, not a rewrite.
    assert repair_wav(wav_path).status == "ok"
    assert wav_path.read_bytes() == before


def test_missing_and_tiny_files(tmp_path: Path) -> None:
    assert inspect_wav(tmp_path / "nope.wav").status == "missing"
    stub = tmp_path / "stub.wav"
    stub.write_bytes(b"RIFF")
    assert inspect_wav(stub).status == "unusable"


def test_non_riff_is_rejected(tmp_path: Path) -> None:
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"NOTAWAVE" + b"\x00" * 100)
    assert inspect_wav(junk).status == "not_riff"


def test_header_without_data_chunk_is_unusable(tmp_path: Path) -> None:
    """A process killed while writing the header leaves exactly this."""
    partial = tmp_path / "partial.wav"
    partial.write_bytes(b"RIFF" + struct.pack("<L", 36) + b"WAVE")
    assert inspect_wav(partial).status == "unusable"


@pytest.mark.parametrize("field", ["data", "riff", "both"])
def test_stale_length_fields_are_repairable(wav_path: Path, field: str) -> None:
    """Both fields are patched separately; a kill between them leaves one stale."""
    _make_wav(wav_path, frames=9_600)
    real = wav_path.read_bytes()

    raw = bytearray(real)
    if field in ("data", "both"):
        raw[40:44] = struct.pack("<L", 1024)
    if field in ("riff", "both"):
        raw[4:8] = struct.pack("<L", 1024)
    wav_path.write_bytes(bytes(raw))
    _age(wav_path)

    assert inspect_wav(wav_path).status == "repairable"
    repaired = repair_wav(wav_path)
    assert repaired.status == "ok"
    assert wav_path.read_bytes() == real, "repair must restore the exact bytes"


def test_trailing_partial_frame_is_truncated(wav_path: Path) -> None:
    _make_wav(wav_path, frames=4_800)
    with open(wav_path, "ab") as fh:
        fh.write(b"\x01\x02\x03")  # less than one 4-byte stereo frame
    _age(wav_path)

    status = inspect_wav(wav_path)
    assert status.status == "repairable"
    repaired = repair_wav(wav_path)

    assert repaired.status == "ok"
    assert wav_path.stat().st_size == 44 + 4_800 * 4
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == 4_800


def test_wav_with_trailing_chunks_is_refused_not_repaired(wav_path: Path) -> None:
    """A foreign WAV with LIST/JUNK after the data chunk must be left alone.

    Everything past the data chunk would otherwise be counted as audio, and a
    forced repair would extend the data chunk over it. Refusing is the only
    safe answer, since telling this apart from a stale length is guesswork.
    """
    _make_wav(wav_path, frames=4_800)
    real_data_bytes = 4_800 * 4

    with open(wav_path, "r+b") as fh:
        fh.seek(0, 2)
        fh.write(b"LIST" + struct.pack("<L", 8) + b"INFOtest")
        total = fh.tell()
        fh.seek(4)
        fh.write(struct.pack("<L", total - 8))  # RIFF covers the new chunk
    _age(wav_path)
    before = wav_path.read_bytes()

    status = inspect_wav(wav_path)
    assert status.status == "unusable"
    assert "follows the data chunk" in status.detail

    with pytest.raises(RuntimeError, match="unusable"):
        repair_wav(wav_path, force=True)
    assert wav_path.read_bytes() == before

    # The audio itself was never in doubt — only what comes after it.
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == real_data_bytes // 4


def test_zero_length_data_with_trailing_chunks_is_refused(tmp_path: Path) -> None:
    """The zero-length-data bypass: a data chunk of 0 bytes followed by real ones.

    An earlier version skipped the trailing-chunk probe whenever the declared
    length was falsy, so this slipped straight through to `repairable` and a
    forced repair would have eaten the LIST chunk.
    """
    odd = tmp_path / "empty-data.wav"
    fmt_body = struct.pack("<HHLLHH", 1, 2, 48000, 192000, 4, 16)
    trailing = b"LIST" + struct.pack("<L", 8) + b"INFOtest"
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<L", 16)
        + fmt_body
        + b"data"
        + struct.pack("<L", 0)   # zero-length data chunk
        + trailing
    )
    odd.write_bytes(b"RIFF" + struct.pack("<L", len(body)) + body)
    _age(odd)
    before = odd.read_bytes()

    status = inspect_wav(odd)
    assert status.status == "unusable"
    assert "follows the data chunk" in status.detail

    with pytest.raises(RuntimeError, match="unusable"):
        repair_wav(odd, force=True)
    assert odd.read_bytes() == before


def test_probe_requires_the_chain_to_land_exactly_on_eof(wav_path: Path) -> None:
    """Bytes that merely look chunk-ish must not be mistaken for a chunk list.

    A truncated recording can easily have four printable bytes at the declared
    end; what it will not have is a chunk chain that accounts for the file to
    the last byte. This keeps the refusal from stealing repairable recordings.
    """
    _make_wav(wav_path, frames=9_600)
    real = wav_path.read_bytes()

    raw = bytearray(real)
    stale_end = 44 + 1024
    raw[40:44] = struct.pack("<L", 1024)
    # Plant a plausible-looking but non-terminating chunk header right there.
    raw[stale_end : stale_end + 8] = b"LIST" + struct.pack("<L", 16)
    wav_path.write_bytes(bytes(raw))
    _age(wav_path)

    # The chain from that point does not end on EOF, so it is not a chunk list.
    assert inspect_wav(wav_path).status == "repairable"


def test_non_canonical_layout_is_refused(tmp_path: Path) -> None:
    """A data chunk somewhere other than offset 44 is not a file we wrote."""
    odd = tmp_path / "odd.wav"
    fmt_body = struct.pack("<HHLLHH", 1, 2, 48000, 192000, 4, 16)
    junk = b"JUNK" + struct.pack("<L", 4) + b"\x00\x00\x00\x00"
    payload = b"\x00" * 400
    body = (
        b"WAVE"
        + junk
        + b"fmt "
        + struct.pack("<L", 16)
        + fmt_body
        + b"data"
        + struct.pack("<L", len(payload))
        + payload
    )
    odd.write_bytes(b"RIFF" + struct.pack("<L", len(body)) + body)

    status = inspect_wav(odd)
    assert status.status == "unusable"
    assert "canonical" in status.detail


def test_stale_length_with_audio_after_it_is_still_repairable(wav_path: Path) -> None:
    """The refusal above must not swallow the case recovery exists for.

    A truncated recording also has bytes past the declared end — but audio,
    not a chunk header. It has to stay repairable.
    """
    _make_wav(wav_path, frames=9_600)
    real = wav_path.read_bytes()
    raw = bytearray(real)
    raw[40:44] = struct.pack("<L", 1024)  # stale: far short of the real length
    wav_path.write_bytes(bytes(raw))
    _age(wav_path)

    assert inspect_wav(wav_path).status == "repairable"
    assert repair_wav(wav_path).status == "ok"
    assert wav_path.read_bytes() == real


def test_dry_run_changes_nothing(wav_path: Path) -> None:
    _make_wav(wav_path)
    raw = bytearray(wav_path.read_bytes())
    raw[40:44] = struct.pack("<L", 7)
    wav_path.write_bytes(bytes(raw))
    _age(wav_path)
    before = wav_path.read_bytes()

    assert repair_wav(wav_path, dry_run=True).status == "repairable"
    assert wav_path.read_bytes() == before


def test_recently_written_file_is_protected(wav_path: Path) -> None:
    """Never repair underneath a recording that may still be running."""
    _make_wav(wav_path)
    raw = bytearray(wav_path.read_bytes())
    raw[40:44] = struct.pack("<L", 7)
    wav_path.write_bytes(bytes(raw))  # mtime = now

    with pytest.raises(RuntimeError, match="may still be running"):
        repair_wav(wav_path)

    assert repair_wav(wav_path, force=True).status == "ok"


# -------------------------------------------------------------------- slides


def test_slides_are_rebuilt_in_order_with_timestamps(meeting_dir: Path) -> None:
    for i, minute in enumerate([0, 5, 11], start=1):
        png = meeting_dir / "slides" / f"slide_{i:03d}.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        stamp = time.mktime((2026, 8, 4, 14, minute, 30, 0, 0, -1))
        os.utime(png, (stamp, stamp))

    slides = rebuild_slides(meeting_dir)
    assert [s["slide_number"] for s in slides] == [1, 2, 3]
    assert [s["timestamp"] for s in slides] == ["14:00:30", "14:05:30", "14:11:30"]
    assert all(s["type"] == "slide" for s in slides)


def test_rebuild_handles_a_directory_with_no_slides(meeting_dir: Path) -> None:
    assert rebuild_slides(meeting_dir) == []


# ------------------------------------------------------------------- recover


def test_recovers_an_interrupted_recording(meeting_dir: Path) -> None:
    """The whole point: a directory postprocess refused becomes one it accepts."""
    _make_wav(meeting_dir / "audio" / "recording.wav", frames=48_000)
    _age(meeting_dir / "audio" / "recording.wav")
    for i in (1, 2):
        (meeting_dir / "slides" / f"slide_{i:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (meeting_dir / "metadata.json").write_text(
        json.dumps(
            {
                "recording_status": "recording",
                "date": "2026-08-04",
                "start_time": "14:00:00",
                "end_time": "14:00:00",
                "duration_seconds": 0,
                "topic": "interrupted",
                "slides": [],
            }
        ),
        encoding="utf-8",
    )

    report = recover_incomplete(meeting_dir)

    meta = json.loads((meeting_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["recording_status"] == "recovered"
    assert meta["slides_count"] == 2
    assert meta["audio_duration_seconds"] == pytest.approx(1.0)
    assert meta["recovery"]["revisits_recovered"] is False
    assert report["slides_rebuilt"] is True

    # postprocess must now accept it.
    from meetingmind.postprocess import _require_finished_recording

    _require_finished_recording(meeting_dir, meta)


def test_recover_does_not_overwrite_a_real_slide_index(meeting_dir: Path) -> None:
    """A recording that got as far as writing its slides knows more than mtimes."""
    _make_wav(meeting_dir / "audio" / "recording.wav")
    _age(meeting_dir / "audio" / "recording.wav")
    (meeting_dir / "slides" / "slide_001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    original = [
        {"type": "slide", "filename": "slide_001.png", "slide_number": 1,
         "timestamp": "09:15:00"},
        {"type": "revisit", "original_slide": 1, "timestamp": "09:20:00"},
    ]
    (meeting_dir / "metadata.json").write_text(
        json.dumps(
            {"recording_status": "recording", "start_time": "09:00:00",
             "slides": original}
        ),
        encoding="utf-8",
    )

    recover_incomplete(meeting_dir)
    meta = json.loads((meeting_dir / "metadata.json").read_text(encoding="utf-8"))

    assert meta["slides"] == original
    assert meta["revisits_count"] == 1  # would be lost by a rebuild


def test_recover_synthesizes_missing_metadata(tmp_path: Path) -> None:
    meeting = tmp_path / "2026-08-04-lost-meeting"
    (meeting / "audio").mkdir(parents=True)
    (meeting / "slides").mkdir()
    _make_wav(meeting / "audio" / "recording.wav", frames=4_800)
    _age(meeting / "audio" / "recording.wav")

    report = recover_incomplete(meeting)
    meta = json.loads((meeting / "metadata.json").read_text(encoding="utf-8"))

    assert report["metadata_synthesized"] is True
    assert meta["date"] == "2026-08-04"
    assert meta["topic"] == "lost-meeting"
    # Must be a real clock string — postprocess would raise TypeError on null.
    assert len(meta["start_time"].split(":")) == 3
    assert meta["recording_status"] == "recovered"


def test_recover_dry_run_writes_nothing(meeting_dir: Path) -> None:
    _make_wav(meeting_dir / "audio" / "recording.wav")
    _age(meeting_dir / "audio" / "recording.wav")
    (meeting_dir / "metadata.json").write_text(
        json.dumps({"recording_status": "recording", "start_time": "10:00:00"}),
        encoding="utf-8",
    )
    before = (meeting_dir / "metadata.json").read_text(encoding="utf-8")

    report = recover_incomplete(meeting_dir, dry_run=True)

    assert report["dry_run"] is True
    assert (meeting_dir / "metadata.json").read_text(encoding="utf-8") == before


def test_recover_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        recover_incomplete(tmp_path / "nowhere")
