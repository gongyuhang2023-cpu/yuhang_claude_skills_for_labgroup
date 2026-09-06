"""Speaker voiceprints — enroll once, then tell whose voice a segment is.

Two-stream 1:1 recordings never needed this: each WAV already held exactly one
voice (see `mic.py`). An **in-person** meeting (`record --mic-only`) puts both
people in one stream, so "who said this" becomes a real question. Diarization
(`diarize.py`) answers *"same voice or different voice"*; this module answers
*"and which of them is you"*.

Why pyannote's embedder and not SpeechBrain's ECAPA
---------------------------------------------------
SpeechBrain and pyannote **cannot share a process**. Lightning's
`model_helpers` calls `inspect.stack()`, whose frame walk touches SpeechBrain's
lazy modules, and one of them (`speechbrain.integrations.k2_fsa`) raises on
import. Swapping the import order only moves the crash — with SpeechBrain first
you instead hit a duplicate `wait_tensor` operator registration. Both were
reproduced on this machine.

pyannote ships `WeSpeakerResNet34` (256-d), and the diarization pass already
returns per-speaker embeddings, so staying inside one library removes the
conflict *and* a dependency. Measured on real recordings: a 60 s solo sample
scores +0.838 against the same voice in a later two-person recording and +0.210
against the other person.

Enrollment is anchored, deliberately
------------------------------------
`enroll` takes audio the caller has **confirmed** contains one known person.
That is not pedantry: identifying a speaker from an unlabelled recording by
picking "the biggest / most cohesive cluster" is wrong often enough to matter.
On this machine's June recordings the *most cohesive* cluster in the user's own
microphone (0.79) was the supervisor's voice bleeding back in through the
speakers — steadier than the user's own voice, because it came from a fixed
position at a fixed level. A voiceprint built that way is silently wrong: it
does not fail, it just identifies the wrong person forever after.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from .audioio import SAMPLE_RATE, load_mono_16k, resolve

if TYPE_CHECKING:  # pragma: no cover
    import torch

EMBEDDING_MODEL = "pyannote/wespeaker-voxceleb-resnet34-LM"

#: Below this, an embedding is computed from too little speech to trust.
MIN_ENROLL_SECONDS = 10.0

#: Above this a cluster is the primary speaker; below it, someone else.
#: One threshold, not a band with an "unsure" gap in the middle: in a meeting
#: between two known people every cluster *is* one of them, so refusing to
#: choose only moves the decision, it does not avoid it. Measured margin on
#: real audio was 0.84 vs 0.21 — the decision is not close.
SPEAKER_THRESHOLD = 0.40


@dataclass(frozen=True)
class Voiceprint:
    """One person's voice, plus enough provenance to audit it later."""

    name: str
    vector: np.ndarray
    source: str
    seconds: float
    model: str = EMBEDDING_MODEL
    created: str = ""

    def similarity(self, other: np.ndarray) -> float:
        return float(_unit(self.vector) @ _unit(other))

    def save(self, path: Path) -> Path:
        path = resolve(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "name": self.name,
                    "vector": [float(x) for x in np.asarray(self.vector).reshape(-1)],
                    "source": self.source,
                    "seconds": round(self.seconds, 2),
                    "model": self.model,
                    "created": self.created or datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> Voiceprint:
        path = resolve(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        stored = data.get("model", EMBEDDING_MODEL)
        if stored != EMBEDDING_MODEL:
            # Embeddings from different models are not comparable at all — the
            # similarity would be a number with no meaning, which is worse than
            # an error because it still looks like an answer.
            raise ValueError(
                f"{path} was enrolled with {stored}, but this build uses "
                f"{EMBEDDING_MODEL}. Re-run `enroll` to rebuild it."
            )
        return cls(
            name=data["name"],
            vector=np.asarray(data["vector"], dtype=np.float32),
            source=data.get("source", ""),
            seconds=float(data.get("seconds", 0.0)),
            model=stored,
            created=data.get("created", ""),
        )


def _unit(v: Any) -> np.ndarray:
    a = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(a))
    if n == 0.0:
        raise ValueError("zero-length embedding — the audio was silent")
    return a / n


class Embedder:
    """Wraps pyannote's speaker embedder. Loaded lazily; reused across calls."""

    def __init__(self, device: str | None = None) -> None:
        self._device = device
        self._inference: Any | None = None

    def _ensure(self) -> Any:
        if self._inference is None:
            import torch
            from pyannote.audio import Inference, Model

            dev = self._device or ("cuda" if torch.cuda.is_available() else "cpu")
            model = Model.from_pretrained(EMBEDDING_MODEL)
            self._inference = Inference(model, window="whole", device=torch.device(dev))
        return self._inference

    def embed(self, waveform: "torch.Tensor") -> np.ndarray:
        """`waveform` is (1, samples) mono at SAMPLE_RATE.

        Feeding a tensor rather than a path is not a preference: pyannote reads
        files through `torchcodec`, which needs FFmpeg shared libraries that are
        not present on Windows (`libtorchcodec_core5.dll` fails to load). Passing
        the waveform skips that path entirely — and skips a second decode.
        """
        inf = self._ensure()
        return _unit(inf({"waveform": waveform, "sample_rate": SAMPLE_RATE}))


def enroll(
    audio_path: Path,
    name: str,
    embedder: Embedder | None = None,
    start: float | None = None,
    end: float | None = None,
) -> Voiceprint:
    """Build `name`'s voiceprint from audio that is **known** to be only them.

    Use `start`/`end` to cut out the part you are sure about. Nothing here
    verifies the claim — see the module docstring for why guessing it is worse
    than asking.
    """
    wav = load_mono_16k(audio_path, start=start, end=end)
    seconds = wav.shape[1] / SAMPLE_RATE
    if seconds < MIN_ENROLL_SECONDS:
        raise ValueError(
            f"only {seconds:.1f}s of audio — enrolment needs at least "
            f"{MIN_ENROLL_SECONDS:.0f}s to be stable."
        )
    emb = (embedder or Embedder()).embed(wav)
    span = "" if start is None and end is None else f"#{start or 0:.0f}-{end or seconds:.0f}"
    return Voiceprint(
        name=name,
        vector=emb,
        source=f"{audio_path}{span}",
        seconds=seconds,
        created=datetime.now().isoformat(timespec="seconds"),
    )


def identify(
    embedding: np.ndarray,
    prints: dict[str, Voiceprint],
    primary: str | None = None,
) -> tuple[str | None, float, dict[str, float]]:
    """Name this embedding. Returns `(name_or_None, confidence, all_scores)`.

    `primary` names the voiceprint that is **best matched to the recording
    conditions** — normally the vault owner, enrolled from the same microphone
    the meeting is recorded on. It is checked on its own threshold rather than
    by `argmax` over every voiceprint.

    The reason is that scores from differently-conditioned voiceprints are not
    on a common scale. A voiceprint enrolled from compressed call audio scores
    lower against in-person audio purely because of the channel, so an argmax
    can hand the wrong name to a segment that the primary check would have
    decided correctly.

    `None` only means "nothing to compare against" — no voiceprints, or a
    non-primary voice with no voiceprint of its own. `resolve_speakers` turns
    that into a name when the group makes it obvious.
    """
    scores = {n: vp.similarity(embedding) for n, vp in prints.items()}
    if not scores:
        return None, 0.0, scores

    if primary and primary in scores:
        others = {n: v for n, v in scores.items() if n != primary}
        if scores[primary] >= SPEAKER_THRESHOLD:
            margin = scores[primary] - max(others.values()) if others else scores[primary]
            return primary, margin, scores
        if others:
            best = max(others, key=others.get)
            if others[best] >= SPEAKER_THRESHOLD:
                return best, others[best] - scores[primary], scores
        # Not the primary, and nobody else is enrolled. Who it is depends on how
        # many voices are in the room — `resolve_speakers` knows that, this does not.
        return None, SPEAKER_THRESHOLD - scores[primary], scores

    best = max(scores, key=scores.get)
    others = [v for n, v in scores.items() if n != best]
    margin = scores[best] - max(others) if others else scores[best]
    return best, margin, scores


@dataclass(frozen=True)
class SpeakerCall:
    """One diarization cluster, named."""

    label: str          # what the diarizer called it, e.g. SPEAKER_00
    name: str | None    # who it is; None = the caller should ask
    confidence: float
    scores: dict[str, float]


def resolve_speakers(
    embeddings: dict[str, np.ndarray],
    prints: dict[str, Voiceprint],
    primary: str | None = None,
    other_name: str | None = None,
) -> list[SpeakerCall]:
    """Name every cluster at once, using what the *set* implies.

    Per-cluster scoring cannot reach the most useful inference in a 1:1 meeting:
    with two voices and one of them matched to you, **the other one is the other
    person** — no voiceprint of them required. That is a statement about the
    group, so it is made here rather than inside `identify`.

    With two clusters and `other_name` given, both always come back named. The
    two people in the room are known; declining to choose between them would
    just hand the same question back, in a transcript instead of a number.
    """
    calls: list[SpeakerCall] = []
    for label, emb in embeddings.items():
        name, conf, scores = identify(emb, prints, primary=primary)
        calls.append(SpeakerCall(label, name, conf, scores))

    if not (primary and other_name and len(calls) == 2):
        return calls

    # Whoever scores higher against the primary is the primary; the other is the
    # other. Ranking sidesteps the threshold entirely, which matters when both
    # land on the same side of it — a threshold cannot separate two clusters,
    # but their order can.
    ranked = sorted(calls, key=lambda c: -c.scores.get(primary, 0.0))
    top, bottom = ranked
    return [
        SpeakerCall(top.label, primary, top.scores.get(primary, 0.0)
                    - bottom.scores.get(primary, 0.0), top.scores),
        SpeakerCall(bottom.label, other_name, top.scores.get(primary, 0.0)
                    - bottom.scores.get(primary, 0.0), bottom.scores),
    ]
