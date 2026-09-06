"""Read audio as the tensor the speech models want, without going through them.

Both pyannote entry points in this package (`diarize`, `voiceprint`) take a
waveform rather than a path, so the decoding happens here, once.

That is not stylistic. pyannote 4 loads files through `torchcodec`, which needs
FFmpeg shared libraries installed system-wide; on this machine it fails outright
(`OSError: Could not load this library: ...libtorchcodec_core5.dll`). Decoding
with `soundfile` sidesteps that dependency — and when a caller diarizes *and*
embeds the same audio, it also saves a second decode of a file that is often
hundreds of megabytes.

`librosa` is deliberately avoided too: its `lazy_loader` calls `inspect.stack()`,
whose frame walk trips other lazily-imported packages in this environment.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio

#: What the speaker models expect. Everything is resampled to this on load.
SAMPLE_RATE = 16_000


def resolve(path: Path | str) -> Path:
    """`~` and `$VAR` expanded, then absolute.

    Every path in this package that can come from a command line goes through
    here. `Path("~/x")` does not expand on its own, and a shell will not expand
    a `~` that sits inside quotes — which is exactly how paths get written in
    documentation. The failure is a late "no such file", long after the caller
    thought the path was fine.
    """
    return Path(os.path.expandvars(str(path))).expanduser()


def duration_seconds(path: Path | str) -> float:
    return float(sf.info(str(resolve(path))).duration)


def load_mono_16k(
    path: Path | str,
    start: float | None = None,
    end: float | None = None,
) -> torch.Tensor:
    """Read `path` (optionally only `start`..`end` seconds) as (1, samples) mono @16 kHz.

    Multi-channel input is averaged down rather than picking channel 0: a
    loopback capture can carry a voice on one side only, and dropping a channel
    would silently drop that voice.
    """
    path = resolve(path)
    info = sf.info(str(path))
    frame_start = 0 if start is None else max(0, int(start * info.samplerate))
    frames = -1 if end is None else max(0, int((end - (start or 0.0)) * info.samplerate))

    y, sr = sf.read(
        str(path),
        start=frame_start,
        frames=frames,
        dtype="float32",
        always_2d=True,
    )
    if y.size == 0:
        raise ValueError(f"no audio in {path} for the requested span")

    wav = torch.from_numpy(np.ascontiguousarray(y.mean(axis=1))).unsqueeze(0)
    if sr != SAMPLE_RATE:
        wav = torchaudio.functional.resample(wav, sr, SAMPLE_RATE)
    return wav


def slice_waveform(wav: torch.Tensor, start: float, end: float) -> torch.Tensor:
    """Cut `start`..`end` (seconds) out of an already-loaded 16 kHz waveform."""
    return wav[:, int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]


def is_effectively_silent(wav: torch.Tensor, rms_floor: float = 1e-3) -> bool:
    """True when there is no signal worth embedding.

    Speaker embedders return a vector for silence too — a confident-looking one,
    pointing wherever the noise floor happens to sit. Callers use this to skip
    those rather than to compare them.
    """
    if wav.numel() == 0:
        return True
    return float(wav.pow(2).mean().sqrt()) < rms_floor
