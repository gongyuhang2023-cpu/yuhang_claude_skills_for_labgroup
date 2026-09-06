"""Finish a meeting directory whose recording never reached ``stop()``.

Since audio is written incrementally, a killed recording usually leaves a WAV
that is already complete and playable — the header is re-patched after every
batch, and the seek that does it flushes the buffer, so what is on disk lags by
at most one batch. This module exists for the remainder:

* the header's two length fields are patched one after the other, so a process
  killed between them leaves them disagreeing (a microsecond window, but a real
  one, and power loss widens it);
* ``metadata.json`` is left saying ``recording_status: "recording"``, with no
  end time, a zero duration and an empty slide index, which `postprocess`
  rightly refuses to touch.

Design notes worth keeping:

* **Nothing here runs automatically.** `postprocess` refuses an unfinished
  recording and prints the command; the user decides. Silently merging three
  sources of truth is the easiest thing in this codebase to get subtly wrong.
* **`inspect_wav` first, and usually that is the end of it.** If the declared
  lengths already match the file, the file is reported ``ok`` and not one byte
  is touched.
* **Repair edits 8 bytes in place.** Writing a corrected copy would mean
  duplicating a multi-gigabyte file, which needs the free space to exist and
  can itself be interrupted — a worse trade for the only copy of a meeting.
* **The slide index is rebuilt from the PNGs**, not from a manifest. The files
  and their mtimes are already on disk, and mtime resolution beats the
  ``%H:%M:%S`` the index stores. Revisit entries cannot be reconstructed; they
  carry no path and nothing downstream computes from them.
"""

from __future__ import annotations

import contextlib
import json
import re
import struct
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .session import write_json_atomic

_METADATA_FILE = "metadata.json"
_AUDIO_REL = Path("audio") / "recording.wav"
_SLIDES_DIRNAME = "slides"

_RIFF_HEADER_BYTES = 12  # "RIFF" + size + "WAVE"
_WAVE_FORMAT_PCM = 0x0001
_MAX_RIFF_SIZE = 0xFFFFFFFF
# RIFF(12) + fmt chunk header(8) + 16-byte fmt body + data chunk header(8).
# Every file WavSink writes puts its audio here and nothing after it.
_CANONICAL_DATA_OFFSET = 44

# A sink that is still writing touches the file constantly; anything older than
# this is safe to assume nobody owns. Crude, but the alternative (locking) does
# not survive the process having been killed.
_QUIESCENT_SECONDS = 5.0

_SLIDE_RE = re.compile(r"slide_(\d+)\.png$", re.IGNORECASE)
_DIRNAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)$")


@dataclass(frozen=True)
class WavStatus:
    """What `inspect_wav` found. `status` drives everything downstream."""

    path: str
    filesize: int
    status: str  # ok | repairable | unusable | not_riff | too_large | missing
    detail: str
    data_offset: int | None = None
    data_declared: int | None = None
    data_actual: int | None = None
    riff_declared: int | None = None
    channels: int | None = None
    samplerate: int | None = None
    block_align: int | None = None

    @property
    def duration_seconds(self) -> float | None:
        if not self.data_actual or not self.samplerate or not self.block_align:
            return None
        return self.data_actual / (self.samplerate * self.block_align)


def _looks_like_chunk_after(path: Path, data_end: int, filesize: int) -> bool:
    """Is there a real RIFF chunk list where the data chunk claims to end?

    Used to tell "a foreign WAV with trailing chunks" from "one of ours whose
    declared length is simply stale". Both have bytes past the declared end;
    only the first has a chunk list sitting there.

    False positives cost a recording that could have been repaired, so the test
    is strict: walk the whole candidate chain (honouring the pad byte on
    odd-sized chunks) and require it to land *exactly* on EOF, with every tag
    printable. Requiring only "the first size fits in the file" was far too
    loose — random PCM produces four printable bytes about 2% of the time, and
    the more audio follows, the likelier a size that happens to fit. Landing
    precisely on EOF is not something arbitrary audio does.
    """
    offset = data_end + (data_end & 1)  # RIFF pads odd-sized chunks
    if offset + 8 > filesize:
        return False

    with open(path, "rb") as fh:
        while offset + 8 <= filesize:
            fh.seek(offset)
            header = fh.read(8)
            if len(header) < 8:
                return False
            tag = header[:4]
            if not all(0x20 <= byte < 0x7F for byte in tag):
                return False
            size = struct.unpack("<L", header[4:8])[0]
            offset += 8 + size + (size & 1)
            if offset == filesize:
                return True  # the chain accounts for the file exactly
            if offset > filesize:
                return False
    return False


def inspect_wav(path: Path) -> WavStatus:
    """Classify a WAV without modifying it.

    Walks the RIFF chunk list rather than assuming the canonical 44-byte
    layout: the assumption happens to hold for files this project writes, but a
    recovery tool that trusts an offset it never verified is exactly the kind
    that corrupts the file it was pointed at.
    """
    path = Path(path)
    if not path.is_file():
        return WavStatus(str(path), 0, "missing", "file does not exist")

    filesize = path.stat().st_size
    if filesize < _RIFF_HEADER_BYTES:
        return WavStatus(str(path), filesize, "unusable", "shorter than a RIFF header")

    with open(path, "rb") as fh:
        head = fh.read(_RIFF_HEADER_BYTES)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return WavStatus(str(path), filesize, "not_riff", "not a RIFF/WAVE file")
        riff_declared = struct.unpack("<L", head[4:8])[0]

        channels = samplerate = block_align = None
        bits = audio_format = None
        data_offset = data_declared = None

        pos = _RIFF_HEADER_BYTES
        while pos + 8 <= filesize:
            fh.seek(pos)
            chunk_head = fh.read(8)
            if len(chunk_head) < 8:
                break
            chunk_id = chunk_head[:4]
            chunk_size = struct.unpack("<L", chunk_head[4:8])[0]

            if chunk_id == b"fmt ":
                body = fh.read(min(16, chunk_size))
                if len(body) < 16:
                    break
                (audio_format, channels, samplerate, _byte_rate, block_align,
                 bits) = struct.unpack("<HHLLHH", body)
            elif chunk_id == b"data":
                data_offset = pos + 8
                data_declared = chunk_size
                break  # this project always writes data last

            pos += 8 + chunk_size + (chunk_size & 1)

    if data_offset is None or channels is None:
        return WavStatus(
            str(path), filesize, "unusable",
            "no fmt/data chunk found (header never completed?)",
            riff_declared=riff_declared,
        )
    if audio_format != _WAVE_FORMAT_PCM or bits != 16:
        return WavStatus(
            str(path), filesize, "unusable",
            f"unsupported format: tag={audio_format} bits={bits}",
            riff_declared=riff_declared,
        )

    payload = filesize - data_offset
    if payload < 0:
        return WavStatus(
            str(path), filesize, "unusable", "data chunk starts past end of file",
            riff_declared=riff_declared,
        )

    # Everything below treats "the rest of the file" as audio. That is only
    # true for the canonical layout this project writes — RIFF, fmt, data, and
    # nothing after it. A file with trailing LIST/JUNK/cue chunks would have
    # them swallowed into the data chunk by a repair, so refuse rather than
    # guess. Two independent signals, both erring towards refusal:
    if data_offset != _CANONICAL_DATA_OFFSET:
        return WavStatus(
            str(path), filesize, "unusable",
            f"data chunk starts at {data_offset}, not the canonical "
            f"{_CANONICAL_DATA_OFFSET}; not a file this tool wrote",
            riff_declared=riff_declared,
        )
    # No truthiness guard on data_declared: a zero-length data chunk followed
    # by real chunks is exactly the case that would otherwise slip through and
    # get them swallowed.
    if data_declared is not None and _looks_like_chunk_after(
        path, data_offset + data_declared, filesize
    ):
        return WavStatus(
            str(path), filesize, "unusable",
            "another RIFF chunk follows the data chunk; repairing would "
            "swallow it. Not a file this tool wrote.",
            riff_declared=riff_declared,
        )
    # Drop a trailing fragment of a frame: a partial frame would shift the
    # channels against each other for anything that read past it.
    data_actual = (payload // block_align) * block_align

    common = {
        "data_offset": data_offset,
        "data_declared": data_declared,
        "data_actual": data_actual,
        "riff_declared": riff_declared,
        "channels": channels,
        "samplerate": samplerate,
        "block_align": block_align,
    }

    if filesize - 8 > _MAX_RIFF_SIZE:
        return WavStatus(
            str(path), filesize, "too_large",
            "beyond the 4 GiB RIFF limit; lengths cannot be expressed",
            **common,
        )

    if data_declared == data_actual and riff_declared == filesize - 8:
        return WavStatus(
            str(path), filesize, "ok", "header already matches the file", **common
        )

    return WavStatus(
        str(path), filesize, "repairable",
        f"header says data={data_declared} riff={riff_declared}; "
        f"file holds data={data_actual} riff={filesize - 8}",
        **common,
    )


def repair_wav(path: Path, *, force: bool = False, dry_run: bool = False) -> WavStatus:
    """Rewrite the two length fields (8 bytes) so they describe the file.

    Refuses a file that was touched within the last few seconds unless forced —
    a live recording is the one thing that must never be repaired underneath
    itself.
    """
    path = Path(path)
    status = inspect_wav(path)
    if status.status == "ok":
        return status
    if status.status != "repairable":
        raise RuntimeError(f"cannot repair {path}: {status.status} — {status.detail}")

    age = time.time() - path.stat().st_mtime
    if age < _QUIESCENT_SECONDS and not force:
        raise RuntimeError(
            f"{path} was written {age:.1f}s ago — a recording may still be "
            f"running. Stop it first, or pass --force."
        )

    assert status.data_offset is not None
    assert status.data_actual is not None

    with open(path, "rb") as fh:
        original_header = fh.read(64)
    print(
        f"[recover] original header (first 64 bytes): {original_header.hex()}",
        file=sys.stderr,
    )

    if dry_run:
        return status

    with open(path, "r+b") as fh:
        end = status.data_offset + status.data_actual
        if end < status.filesize:
            fh.truncate(end)
        fh.seek(status.data_offset - 4)
        fh.write(struct.pack("<L", status.data_actual))
        fh.seek(4)
        fh.write(struct.pack("<L", end - 8))
        fh.flush()

    after = inspect_wav(path)
    if after.status != "ok":
        raise RuntimeError(f"repair did not settle: {after.status} — {after.detail}")
    return after


def rebuild_slides(meeting_dir: Path) -> list[dict[str, Any]]:
    """Reconstruct the slide index from the PNGs and their mtimes.

    Revisit entries are gone for good; they hold no path and nothing downstream
    computes from them, so the index is complete for every practical purpose.
    """
    slides_dir = Path(meeting_dir) / _SLIDES_DIRNAME
    if not slides_dir.is_dir():
        return []

    found: list[tuple[int, Path]] = []
    for png in slides_dir.glob("slide_*.png"):
        match = _SLIDE_RE.search(png.name)
        if match:
            found.append((int(match.group(1)), png))
    found.sort()

    return [
        {
            "type": "slide",
            "filename": png.name,
            "slide_number": number,
            "timestamp": datetime.fromtimestamp(png.stat().st_mtime).strftime(
                "%H:%M:%S"
            ),
            "filepath": str(png),
        }
        for number, png in found
    ]


def _synthesize_metadata(meeting_dir: Path) -> dict[str, Any]:
    """Build a minimal metadata dict when the file is missing entirely.

    `start_time` must be a valid ``%H:%M:%S`` string: postprocess feeds it to
    `strptime` and only catches ValueError, so a null would be a TypeError that
    takes the whole run down.
    """
    date = ""
    topic = meeting_dir.name
    if (match := _DIRNAME_RE.match(meeting_dir.name)) is not None:
        date, topic = match.group(1), match.group(2)

    anchor: float | None = None
    first_slide = sorted((meeting_dir / _SLIDES_DIRNAME).glob("slide_*.png"))
    if first_slide:
        anchor = first_slide[0].stat().st_mtime
    elif (meeting_dir / _AUDIO_REL).is_file():
        anchor = (meeting_dir / _AUDIO_REL).stat().st_mtime
    started = datetime.fromtimestamp(anchor) if anchor else datetime.now()

    return {
        "recording_status": "recording",
        "date": date or started.strftime("%Y-%m-%d"),
        "start_time": started.strftime("%H:%M:%S"),
        "end_time": started.strftime("%H:%M:%S"),
        "duration_seconds": 0,
        "topic": topic,
        "audio_path": str(_AUDIO_REL).replace("\\", "/"),
        "slides": [],
        "synthesized": True,
    }


def recover_incomplete(
    meeting_dir: Path, *, force: bool = False, dry_run: bool = False
) -> dict[str, Any]:
    """Bring a half-finished meeting directory to ``recording_status=recovered``.

    Returns a report describing what was done. With ``dry_run`` nothing is
    written and the report says what would have been.
    """
    meeting_dir = Path(meeting_dir)
    if not meeting_dir.is_dir():
        raise FileNotFoundError(f"meeting directory not found: {meeting_dir}")

    meta_path = meeting_dir / _METADATA_FILE
    if meta_path.is_file():
        metadata: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
        synthesized = False
    else:
        metadata = _synthesize_metadata(meeting_dir)
        synthesized = True

    wav_path = meeting_dir / _AUDIO_REL
    wav_status = inspect_wav(wav_path)
    repaired = False
    if wav_status.status == "repairable":
        wav_status = repair_wav(wav_path, force=force, dry_run=dry_run)
        repaired = not dry_run

    # Only rebuild when the index is actually missing: a recording that got as
    # far as writing its slides knows more than mtimes ever will.
    slides_rebuilt = False
    if not metadata.get("slides"):
        rebuilt = rebuild_slides(meeting_dir)
        if rebuilt:
            metadata["slides"] = rebuilt
            slides_rebuilt = True

    slides = metadata.get("slides", [])
    metadata["slides_count"] = sum(1 for s in slides if s.get("type") == "slide")
    metadata["revisits_count"] = sum(1 for s in slides if s.get("type") == "revisit")

    if wav_status.duration_seconds is not None:
        metadata["audio_duration_seconds"] = round(wav_status.duration_seconds, 3)
        if wav_status.samplerate:
            metadata["audio_sample_rate"] = wav_status.samplerate
        if wav_status.channels:
            metadata["audio_channels"] = wav_status.channels

    # The last write to the WAV is the best available answer to "when did this
    # actually stop" — better than the clock now, which includes however long
    # the directory sat there before anyone ran recovery.
    if wav_path.is_file():
        ended = datetime.fromtimestamp(wav_path.stat().st_mtime)
        metadata["end_time"] = ended.strftime("%H:%M:%S")
        with contextlib.suppress(ValueError, TypeError):
            started = datetime.strptime(metadata["start_time"], "%H:%M:%S")
            delta = (
                ended
                - ended.replace(
                    hour=started.hour, minute=started.minute, second=started.second,
                    microsecond=0,
                )
            ).total_seconds()
            if delta >= 0:
                metadata["duration_seconds"] = int(delta)

    metadata["recording_status"] = "recovered"
    metadata["recovery"] = {
        "recovered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "wav_status": wav_status.status,
        "wav_detail": wav_status.detail,
        "wav_repaired": repaired,
        "metadata_synthesized": synthesized,
        "slides_rebuilt": slides_rebuilt,
        # Revisit records live only in memory while recording; nothing on disk
        # remembers them, so a recovered index never has any.
        "revisits_recovered": False,
    }

    if not dry_run:
        write_json_atomic(meta_path, metadata)

    return {
        "meeting_dir": str(meeting_dir),
        "metadata_path": str(meta_path),
        "dry_run": dry_run,
        "wav": asdict(wav_status),
        "wav_repaired": repaired,
        "metadata_synthesized": synthesized,
        "slides_rebuilt": slides_rebuilt,
        "slides_count": metadata["slides_count"],
        "audio_duration_seconds": metadata.get("audio_duration_seconds"),
        "recording_status": metadata["recording_status"],
    }
