"""Speaker diarization — split one microphone's audio into "who spoke when".

Needed only by the in-person path (`record --mic-only`). Online calls record one
stream per person and get speaker labels for free (`mic.py`); a room microphone
does not, so the split has to be inferred from the voices themselves.

Turns come out of pyannote's **exclusive** annotation, not the overlap-aware
one: each transcript segment ends up with a single speaker, so an assignment
that can hand back two answers for one span is not useful here. Overlapped
speech still exists in the audio, and shows up as the two speakers' turns
meeting at a boundary rather than as a labelled overlap.

Requires `pyannote.audio` plus a Hugging Face token with access to the (gated,
free) `speaker-diarization-community-1` pipeline. Absent either, `available()`
is False and callers fall back to an unlabelled transcript — a meeting with no
speaker labels is still a usable meeting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .audioio import SAMPLE_RATE, load_mono_16k, resolve

if TYPE_CHECKING:  # pragma: no cover
    import torch

PIPELINE = "pyannote/speaker-diarization-community-1"

#: Turns shorter than this are dropped before they reach the transcript.
#: Onsets and clicks produce sub-100 ms "turns" — on a real 2-minute recording
#: the first second alone yielded eight of them, 0.02–0.14 s each. Kept, they
#: read as speaker changes that never happened.
MIN_TURN_SECONDS = 0.30


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: str

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlap(self, start: float, end: float) -> float:
        return max(0.0, min(self.end, end) - max(self.start, start))


def available() -> bool:
    try:
        import pyannote.audio  # noqa: F401
    except Exception:
        return False
    return True


def diarize(
    audio_path: Path,
    num_speakers: int | None = None,
    min_turn: float = MIN_TURN_SECONDS,
    device: str | None = None,
    waveform: "torch.Tensor | None" = None,
) -> list[Turn]:
    """Return the turns in `audio_path`, ordered by start time.

    Pass `num_speakers` when the count is known — an in-person 1:1 is always 2,
    and saying so is more reliable than letting the pipeline estimate it.

    `waveform` lets a caller that has already decoded the file hand it over
    instead of paying for a second decode.
    """
    import torch
    from pyannote.audio import Pipeline

    dev = device or ("cuda" if torch.cuda.is_available() else "cpu")
    pipeline = Pipeline.from_pretrained(PIPELINE).to(torch.device(dev))

    wav = waveform if waveform is not None else load_mono_16k(resolve(audio_path))
    # Waveform in, not a path — see `audioio` for why the file path route is
    # unusable here.
    output = pipeline({"waveform": wav, "sample_rate": SAMPLE_RATE},
                      num_speakers=num_speakers)

    annotation = output.exclusive_speaker_diarization
    turns = [
        Turn(float(seg.start), float(seg.end), str(label))
        for seg, _, label in annotation.itertracks(yield_label=True)
    ]
    turns = [t for t in turns if t.duration >= min_turn]
    turns.sort(key=lambda t: t.start)
    return turns


def speakers(turns: list[Turn]) -> list[str]:
    """Speaker labels, most talkative first."""
    totals: dict[str, float] = {}
    for t in turns:
        totals[t.speaker] = totals.get(t.speaker, 0.0) + t.duration
    return sorted(totals, key=lambda s: -totals[s])


def speaking_time(turns: list[Turn]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in turns:
        out[t.speaker] = out.get(t.speaker, 0.0) + t.duration
    return out


def speaker_blocks(
    turns: list[Turn],
    gap_tolerance: float = 0.6,
    max_seconds: float | None = None,
) -> list[Turn]:
    """Merge consecutive turns by the same speaker into blocks to transcribe.

    Diarization cuts at every pause, so one person's sentence arrives as several
    turns. Sending each of those to the recogniser separately would transcribe
    fragments with no context; merging them back restores whole utterances while
    keeping the speaker boundaries, which are the part that must not be lost.

    `gap_tolerance` is how long a silence may be and still count as the same
    person still talking. `max_seconds` caps a block so an uninterrupted
    monologue does not become one enormous recognition call.
    """
    if not turns:
        return []
    ordered = sorted(turns, key=lambda t: t.start)
    blocks = [Turn(ordered[0].start, ordered[0].end, ordered[0].speaker)]
    for t in ordered[1:]:
        last = blocks[-1]
        same = t.speaker == last.speaker and (t.start - last.end) <= gap_tolerance
        too_long = max_seconds is not None and (t.end - last.start) > max_seconds
        if same and not too_long:
            blocks[-1] = Turn(last.start, max(last.end, t.end), last.speaker)
        else:
            blocks.append(Turn(t.start, t.end, t.speaker))
    return blocks


def assign(turns: list[Turn], start: float, end: float) -> str | None:
    """Which speaker owns the span `start`..`end`?

    Whoever holds the most of it. A transcript segment routinely spans a
    hand-over — the answer wanted is "whose segment is this mostly", not "who
    was talking at its first instant", which would follow the previous speaker
    into the next person's sentence.
    """
    if not turns:
        return None
    best, best_overlap = None, 0.0
    for t in turns:
        ov = t.overlap(start, end)
        if ov > best_overlap:
            best, best_overlap = t.speaker, ov
    return best
