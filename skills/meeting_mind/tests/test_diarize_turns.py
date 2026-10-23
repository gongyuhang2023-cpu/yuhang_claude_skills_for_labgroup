"""Test K-diarize — turn bookkeeping: filtering, merging, and attribution.

No model runs here. What is pinned is the arithmetic between pyannote's output
and the transcript, which is where the speaker labels are actually won or lost:
a correct diarization can still produce a wrong transcript if the turns are
merged or attributed badly.
"""

from __future__ import annotations

import pytest

from meetingmind.diarize import (
    MIN_TURN_SECONDS,
    Turn,
    assign,
    speaker_blocks,
    speakers,
    speaking_time,
)


def t(start: float, end: float, who: str) -> Turn:
    return Turn(start, end, who)


# ------------------------------------------------------------------ overlap
def test_overlap_is_the_shared_span() -> None:
    assert t(0, 10, "A").overlap(5, 20) == pytest.approx(5.0)
    assert t(0, 10, "A").overlap(10, 20) == 0.0       # touching is not overlapping
    assert t(0, 10, "A").overlap(20, 30) == 0.0


# ------------------------------------------------------------------ assign
def test_assign_picks_the_majority_holder_not_the_first_voice() -> None:
    """A transcript segment routinely straddles a hand-over.

    Taking whoever was talking at its first instant would follow the previous
    speaker into the next person's sentence — every time the recogniser's cut
    lands mid-exchange.
    """
    turns = [t(0, 6, "A"), t(6, 20, "B")]
    assert assign(turns, 5.0, 20.0) == "B"      # A holds 1s of it, B holds 14
    assert assign(turns, 0.0, 6.5) == "A"


def test_assign_returns_none_when_the_span_touches_nothing() -> None:
    assert assign([t(0, 5, "A")], 10.0, 20.0) is None
    assert assign([], 0.0, 5.0) is None


# ------------------------------------------------------------------ blocks
def test_blocks_merge_the_same_voice_across_a_breath() -> None:
    """One person's sentence arrives as several turns — pauses are cut points.

    Transcribing those separately would feed the recogniser fragments with no
    context, which is measurably worse than giving it the whole utterance.
    """
    turns = [t(0, 4, "A"), t(4.3, 9, "A"), t(9.2, 14, "A")]
    blocks = speaker_blocks(turns, gap_tolerance=0.6)
    assert len(blocks) == 1
    assert (blocks[0].start, blocks[0].end, blocks[0].speaker) == (0, 14, "A")


def test_blocks_do_not_merge_across_a_long_silence() -> None:
    turns = [t(0, 4, "A"), t(30, 34, "A")]
    assert len(speaker_blocks(turns, gap_tolerance=0.6)) == 2


def test_blocks_never_merge_different_voices() -> None:
    """The whole point. Merging here would put two people under one label."""
    turns = [t(0, 4, "A"), t(4.05, 8, "B"), t(8.05, 12, "A")]
    blocks = speaker_blocks(turns, gap_tolerance=5.0)   # generous on purpose
    assert [b.speaker for b in blocks] == ["A", "B", "A"]


def test_blocks_are_capped_so_a_monologue_stays_transcribable() -> None:
    turns = [t(i * 10, i * 10 + 9.8, "A") for i in range(10)]   # 100s straight
    blocks = speaker_blocks(turns, gap_tolerance=1.0, max_seconds=30.0)
    assert len(blocks) > 1
    assert all(b.end - b.start <= 40.0 for b in blocks)
    assert all(b.speaker == "A" for b in blocks)


def test_blocks_sort_before_merging() -> None:
    turns = [t(9.2, 14, "A"), t(0, 4, "A"), t(4.3, 9, "A")]
    blocks = speaker_blocks(turns, gap_tolerance=0.6)
    assert len(blocks) == 1 and blocks[0].end == 14


def test_no_turns_no_blocks() -> None:
    assert speaker_blocks([]) == []


# ------------------------------------------------------------------ totals
def test_speakers_ordered_by_time_spoken() -> None:
    turns = [t(0, 2, "A"), t(2, 20, "B"), t(20, 23, "A")]
    assert speakers(turns) == ["B", "A"]
    assert speaking_time(turns) == pytest.approx({"A": 5.0, "B": 18.0})


def test_min_turn_threshold_is_meaningfully_above_zero() -> None:
    """Onsets and clicks arrive as sub-100 ms "turns".

    On a real two-minute recording the first second alone produced eight of
    them (0.02–0.14 s). Left in, each reads as a speaker change that never
    happened — and every one of them would become its own recognition call.
    """
    assert 0.1 <= MIN_TURN_SECONDS <= 1.0
