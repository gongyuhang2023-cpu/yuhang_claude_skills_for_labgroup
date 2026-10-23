"""Test L-voiceprint — storage round-trip and the naming rules.

Synthetic vectors throughout: what is being pinned is the decision logic, not
the embedder. The rules exist because of specific failures observed on real
recordings, and each test names the one it guards.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from meetingmind.voiceprint import (
    EMBEDDING_MODEL,
    SPEAKER_THRESHOLD,
    Voiceprint,
    identify,
    resolve_speakers,
)


def vec(*values: float) -> np.ndarray:
    v = np.zeros(8, dtype=np.float32)
    for i, x in enumerate(values):
        v[i] = x
    return v / np.linalg.norm(v)


ME = vec(1.0)
HIM = vec(0.0, 1.0)
MOSTLY_ME = vec(1.0, 0.35)          # ~0.94 with ME
FAINT = vec(0.35, 0.937)            # ~0.35 with ME — below the threshold


def vp(name: str, v: np.ndarray) -> Voiceprint:
    return Voiceprint(name=name, vector=v, source="test", seconds=60.0)


# ------------------------------------------------------------------ storage
def test_save_load_round_trip(tmp_path: Path) -> None:
    out = vp("我", ME).save(tmp_path / "me.json")
    back = Voiceprint.load(out)
    assert back.name == "我"
    assert np.allclose(back.vector, ME)
    assert back.model == EMBEDDING_MODEL


def test_loading_a_voiceprint_from_another_model_is_refused(tmp_path: Path) -> None:
    """Cosine similarity between two models' embeddings is a number with no
    meaning — worse than an error, because it still looks like an answer."""
    p = tmp_path / "old.json"
    p.write_text(json.dumps({
        "name": "我", "vector": [float(x) for x in ME],
        "model": "speechbrain/spkrec-ecapa-voxceleb",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="enrolled with"):
        Voiceprint.load(p)


def test_paths_with_a_tilde_are_expanded(tmp_path: Path, monkeypatch) -> None:
    """`~/…` has to work, because that is how paths get written down.

    A shell leaves `~` alone inside quotes and `Path` never expands it, so a
    documented command copied verbatim fails on a file that is plainly there.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    home_file = tmp_path / "prints" / "me.json"
    vp("我", ME).save(home_file)

    assert Voiceprint.load("~/prints/me.json").name == "我"


def test_save_expands_too(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    out = vp("我", ME).save("~/out/me.json")
    assert out.is_absolute() and out.is_file()
    assert "~" not in str(out)


# ------------------------------------------------------------------ identify
def test_primary_is_decided_on_its_own_threshold() -> None:
    name, conf, _ = identify(MOSTLY_ME, {"我": vp("我", ME)}, primary="我")
    assert name == "我" and conf > 0


def test_clearly_not_the_primary_is_not_silently_named() -> None:
    """With only one voiceprint, "not you" is not yet "them" — that inference
    needs to know how many people are in the room, which `identify` does not."""
    name, _, _ = identify(HIM, {"我": vp("我", ME)}, primary="我")
    assert name is None


def test_below_the_threshold_identify_alone_says_nothing() -> None:
    """`identify` sees one embedding, so "not the primary" is as far as it goes.

    Who it actually is depends on how many voices are in the room, which only
    `resolve_speakers` knows.
    """
    name, _, scores = identify(FAINT, {"我": vp("我", ME)}, primary="我")
    assert scores["我"] < SPEAKER_THRESHOLD
    assert name is None


def test_primary_wins_over_a_higher_scoring_non_primary() -> None:
    """Scores from differently-conditioned voiceprints are not comparable.

    An argmax lets a voiceprint enrolled elsewhere outrank the one recorded on
    this very microphone — the observed failure mode is a *confident* wrong
    name, which is exactly what must not reach the meeting notes.
    """
    prints = {"我": vp("我", ME), "他": vp("他", MOSTLY_ME)}
    name, _, scores = identify(MOSTLY_ME, prints, primary="我")
    assert scores["他"] > scores["我"]
    assert name == "我"


def test_no_voiceprints_means_no_name() -> None:
    assert identify(ME, {})[0] is None


# ------------------------------------------------------------------ resolve
def test_two_voices_one_of_them_you_names_the_other() -> None:
    """The inference `identify` cannot make alone: with exactly two people and
    one confidently matched, the remaining voice needs no voiceprint."""
    calls = resolve_speakers(
        {"SPEAKER_00": MOSTLY_ME, "SPEAKER_01": HIM},
        {"我": vp("我", ME)}, primary="我", other_name="师兄",
    )
    got = {c.label: c.name for c in calls}
    assert got == {"SPEAKER_00": "我", "SPEAKER_01": "师兄"}


def test_without_a_name_for_the_other_it_stays_unnamed() -> None:
    """No `--other` means nothing to call them — the diarizer's label stands."""
    calls = resolve_speakers(
        {"SPEAKER_00": MOSTLY_ME, "SPEAKER_01": HIM},
        {"我": vp("我", ME)}, primary="我",
    )
    got = {c.label: c.name for c in calls}
    assert got["SPEAKER_00"] == "我" and got["SPEAKER_01"] is None


def test_two_clusters_both_above_the_threshold_are_still_told_apart() -> None:
    """Two people in the room means two names, even when both score high.

    A threshold cannot separate two clusters that sit on the same side of it —
    their order can. Ranking against the primary is what decides.
    """
    calls = resolve_speakers(
        {"A": ME, "B": MOSTLY_ME},
        {"我": vp("我", ME)}, primary="我", other_name="师兄",
    )
    got = {c.label: c.name for c in calls}
    assert got == {"A": "我", "B": "师兄"}


def test_two_voices_are_named_even_when_neither_matches_well() -> None:
    """Recording conditions can drag every score down. With two known people
    the answer is still one each — the ranking survives what the threshold
    would not."""
    calls = resolve_speakers(
        {"A": FAINT, "B": HIM},
        {"我": vp("我", ME)}, primary="我", other_name="师兄",
    )
    got = {c.label: c.name for c in calls}
    assert got == {"A": "我", "B": "师兄"}


def test_three_speakers_do_not_trigger_the_two_person_shortcut() -> None:
    """"Not you" only means "the other one" when there *is* exactly one other."""
    calls = resolve_speakers(
        {"A": MOSTLY_ME, "B": HIM, "C": vec(0.0, 0.0, 1.0)},
        {"我": vp("我", ME)}, primary="我", other_name="师兄",
    )
    got = {c.label: c.name for c in calls}
    assert got["A"] == "我"
    assert got["B"] is None and got["C"] is None
