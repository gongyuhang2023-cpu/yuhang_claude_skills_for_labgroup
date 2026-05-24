"""Local-GPU audio transcription via Qwen3-ASR-1.7B.

`Transcriber` converts a recorded `.wav` (any sample rate / channel layout
librosa can read) into a list of `Segment`s and renders them as a markdown
transcript with `[HH:MM:SS]` timestamps.

The class is intentionally lazy: constructing it does no heavy work. The
underlying qwen-asr model is only loaded on the first call that needs it
(via the `.model` property). This keeps the CLI startup fast — listing
`--help` or arg-validating doesn't pay the multi-second model load.

Public API:
  - `Segment` — frozen dataclass with start/end seconds and text
  - `Transcriber(model_id, device)` — main entry point
  - `DEFAULT_MODEL_ID` / `DEFAULT_CHUNK_MINUTES` — module constants

Why no module-level torch import:
  Importing torch at module load time adds ~1–2s of overhead and pulls
  CUDA initialization eagerly. We push the import inside `_is_cuda_available`
  and `_load_model` so unit tests (and the CLI's --help path) never pay it.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid pulling numpy at import time
    import numpy as np

DEFAULT_MODEL_ID: str = "Qwen/Qwen3-ASR-1.7B"
DEFAULT_CHUNK_MINUTES: float = 15.0

_VALID_DEVICES: frozenset[str] = frozenset({"auto", "cuda", "cpu"})


@dataclass(frozen=True)
class Segment:
    """One transcribed audio span. Timestamps are seconds from recording start."""

    start: float
    end: float
    text: str


# --------------------------------------------------------------------- helpers


def _is_cuda_available() -> bool:
    """Lazy CUDA probe — imports torch only on call. Patched in unit tests."""
    import torch  # noqa: PLC0415 — deliberate lazy import

    return bool(torch.cuda.is_available())


def _select_device(requested: str) -> str:
    """Resolve a requested device string to one of {"cuda", "cpu"}.

    Behavior:
      - "auto"  → cuda if available, else cpu (silent on miss; "auto" implies
                  "do whatever works", no fallback warning warranted).
      - "cuda"  → cuda if available, else cpu with a stderr fallback warning.
      - "cpu"   → cpu, with a stderr warning that ASR will be 5–10x slower.

    Raises ValueError for any other value so typos surface immediately rather
    than silently degrading to cpu.
    """
    req = requested.lower().strip()
    if req not in _VALID_DEVICES:
        raise ValueError(
            f"Invalid device '{requested}'. Expected one of {sorted(_VALID_DEVICES)}."
            f" / 无效设备参数 '{requested}'，可选 {sorted(_VALID_DEVICES)}。"
        )

    if req == "cpu":
        print(
            "[transcribe] device=cpu requested; expect 5-10x slower than GPU. "
            "/ 已选 CPU 推理，预计比 GPU 慢 5-10 倍。",
            file=sys.stderr,
        )
        return "cpu"

    has_cuda = _is_cuda_available()
    if req == "cuda" and not has_cuda:
        print(
            "[transcribe] CUDA requested but not available — fallback to CPU. "
            "/ 请求 CUDA 但当前不可用，已回退到 CPU。",
            file=sys.stderr,
        )
        return "cpu"
    if req == "auto" and not has_cuda:
        # Auto mode is intentionally quiet on the happy path; one short note
        # so the user understands which device is in use.
        print(
            "[transcribe] auto-selected CPU (no CUDA detected). "
            "/ 自动选择 CPU（未检测到 CUDA）。",
            file=sys.stderr,
        )
        return "cpu"

    return "cuda"


def format_timestamp_plain(seconds: float) -> str:
    """Render a non-negative second count as ``HH:MM:SS`` (floor, never round).

    Public helper for callers that want the time string *without* surrounding
    brackets — e.g. ai_input.json fields, signed-offset rendering. Returns
    ``"00:00:00"`` for negative input (clamped to zero) so the function is
    total over the float domain.
    """
    if seconds < 0:
        seconds = 0.0
    total = int(seconds)  # floor toward zero — `int(59.9) == 59`
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_timestamp(seconds: float) -> str:
    """Render a non-negative second count as ``[HH:MM:SS]`` for transcript markers.

    Wraps :func:`format_timestamp_plain` with the bracket style used inside
    ``transcript.md``. Use the plain variant for JSON / signed-offset output
    where brackets would be syntactic noise.
    """
    return f"[{format_timestamp_plain(seconds)}]"


def _chunk_audio_indices(
    total_seconds: float, chunk_seconds: float
) -> list[tuple[float, float]]:
    """Plan how to slice a `total_seconds`-long audio into fixed-size chunks.

    The last chunk is naturally shorter than `chunk_seconds` when the total
    isn't a multiple. An empty or non-positive duration yields no chunks.

    Raises ValueError if `chunk_seconds` is non-positive — a zero-length chunk
    would loop forever.
    """
    if chunk_seconds <= 0:
        raise ValueError(
            f"chunk_seconds must be > 0, got {chunk_seconds}"
            f" / 分块时长必须 > 0，当前 {chunk_seconds}"
        )
    if total_seconds <= 0:
        return []

    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start < total_seconds:
        end = min(start + chunk_seconds, total_seconds)
        chunks.append((start, end))
        start = end
    return chunks


_TARGET_SAMPLE_RATE: int = 16000


def _resample_to_16k_mono(audio_path: Path) -> tuple[np.ndarray, int]:
    """Load `audio_path` as 16 kHz mono float32. Returns ``(audio, 16000)``.

    Accepts any sample rate / channel count that librosa can decode; the
    resampling and mono-mixdown happen inside librosa.load. A non-existent
    path raises FileNotFoundError before librosa is even imported; any
    decode error is wrapped in a bilingual RuntimeError so the caller sees
    a meaningful message instead of a librosa internal traceback.
    """
    if not audio_path.exists():
        raise FileNotFoundError(
            f"Audio file not found: {audio_path}"
            f" / 找不到音频文件：{audio_path}"
        )

    import librosa  # noqa: PLC0415 — lazy import keeps module load fast

    try:
        audio, _ = librosa.load(str(audio_path), sr=_TARGET_SAMPLE_RATE, mono=True)
    except Exception as exc:  # noqa: BLE001 — librosa wraps audioread/sf errors
        raise RuntimeError(
            f"Failed to decode {audio_path}: {exc}"
            f" / 解码失败 {audio_path}：{exc}"
        ) from exc
    return audio, _TARGET_SAMPLE_RATE


def _save_chunk_wav(
    audio: np.ndarray,
    sr: int,
    start_sec: float,
    end_sec: float,
    output_path: Path,
) -> None:
    """Write ``audio[start_sec:end_sec]`` to a WAV file at ``output_path``.

    Negative `start_sec` is clamped to 0; `end_sec` past the audio end is
    clamped to the available length. A zero-length slice writes a valid
    empty WAV — useful so downstream code can treat "nothing to transcribe"
    uniformly.
    """
    import soundfile as sf  # noqa: PLC0415

    start_idx = max(0, int(start_sec * sr))
    end_idx = min(len(audio), int(end_sec * sr))
    if end_idx < start_idx:
        end_idx = start_idx
    chunk = audio[start_idx:end_idx]
    sf.write(str(output_path), chunk, sr)


def _format_transcript_md(segments: list[Segment]) -> str:
    """Render segments as a UTF-8 markdown transcript with timestamp prefixes."""
    lines: list[str] = ["# 会议转录", ""]
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        lines.append(f"{format_timestamp(seg.start)} {text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_transcript(segments: list[Segment], output_path: Path) -> None:
    """Write `segments` to `output_path` as a markdown transcript (UTF-8).

    Creates parent directories if they don't exist. Always writes a valid
    file even for an empty segment list — the result will be just the
    `# 会议转录` header, useful as a sentinel that transcription ran but
    produced nothing (e.g. silent recording).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_format_transcript_md(segments), encoding="utf-8")


# --------------------------------------------------------------------- class


class Transcriber:
    """Wraps Qwen3-ASR-1.7B for offline meeting transcription.

    Construction is cheap. The model is downloaded (if needed) and pushed to
    the resolved device on the first access of `.model` or first call to
    `.transcribe()`. Repeated calls reuse the cached model.
    """

    def __init__(
        self, model_id: str = DEFAULT_MODEL_ID, device: str = "auto"
    ) -> None:
        self._model_id = model_id
        self._device = _select_device(device)
        self._model: Any = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def device(self) -> str:
        return self._device

    @property
    def model(self) -> Any:
        """Lazy accessor — loads on first use, caches thereafter."""
        if self._model is None:
            self._model = self._load_model()
        return self._model

    def _load_model(self) -> Any:
        """Load Qwen3-ASR onto ``self._device`` via the qwen-asr wrapper.

        On CUDA OOM at load time, retries once on CPU and rebinds the
        resolved device — that case is rare with a 1.7B model on modern
        GPUs but is cheap to handle gracefully. A missing [transcribe]
        extras package surfaces as a friendly install-command hint rather
        than a raw ModuleNotFoundError. Any other failure (network cut,
        model ID typo, dependency mismatch) is wrapped in a bilingual
        RuntimeError so the caller doesn't see a raw transformers stack.
        """
        # Imports are inside the try block so a missing [transcribe] extras
        # package (ImportError) is caught alongside model-load failures.
        try:
            import torch  # noqa: PLC0415
            from qwen_asr import Qwen3ASRModel  # noqa: PLC0415

            return Qwen3ASRModel.from_pretrained(
                self._model_id, device_map=self._device
            )
        except ImportError as exc:
            raise RuntimeError(
                f"Transcription dependencies missing: {exc}. "
                f'Install with: pip install -e ".[transcribe]" '
                f"--extra-index-url https://download.pytorch.org/whl/cu128"
                f" / 转录依赖未安装：{exc}。"
                f'请运行：pip install -e ".[transcribe]" '
                f"--extra-index-url https://download.pytorch.org/whl/cu128"
            ) from exc
        except torch.cuda.OutOfMemoryError as exc:
            if self._device != "cuda":
                raise
            print(
                f"[transcribe] CUDA OOM on model load — retrying on CPU. ({exc})"
                f" / CUDA 显存不足，回退 CPU 重试。({exc})",
                file=sys.stderr,
            )
            self._device = "cpu"
            return Qwen3ASRModel.from_pretrained(self._model_id, device_map="cpu")
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load model {self._model_id} on {self._device}: {exc}"
                f" / 模型加载失败 {self._model_id} on {self._device}：{exc}"
            ) from exc

    def transcribe(
        self,
        audio_path: Path,
        vocab_context: str = "",
        chunk_minutes: float = DEFAULT_CHUNK_MINUTES,
    ) -> list[Segment]:
        """Transcribe ``audio_path`` into a list of `Segment` (one per chunk).

        Pipeline:
          1. Resample to 16 kHz mono via librosa.
          2. Plan chunks of ``chunk_minutes`` minutes (last chunk may be short).
          3. For each chunk, write a tmp WAV and call qwen-asr.transcribe with
             the same ``vocab_context`` (so hot-words apply uniformly).
          4. Collapse each chunk's results into one `Segment` whose `start`
             and `end` are the chunk boundaries and whose `text` is the
             concatenation of every ASRTranscription's text from that chunk.

        Why per-chunk granularity (Path B): qwen-asr 0.0.6 only exposes
        per-utterance timestamps via the Qwen3-ForcedAligner companion
        model, which would require an extra ~GB download and triples
        inference time. For meeting transcripts the slide-capture metadata
        already gives precise timestamps that can be cross-referenced
        against the chunked transcript for AI-summary correlation.
        """
        audio, sr = _resample_to_16k_mono(audio_path)
        total_seconds = len(audio) / sr if sr > 0 else 0.0
        chunk_seconds = chunk_minutes * 60.0
        plan = _chunk_audio_indices(total_seconds, chunk_seconds)
        if not plan:
            return []

        segments: list[Segment] = []
        with tempfile.TemporaryDirectory(prefix="meetingmind_chunks_") as tmp:
            tmp_dir = Path(tmp)
            for idx, (start_sec, end_sec) in enumerate(plan, start=1):
                chunk_path = tmp_dir / f"chunk_{idx:03d}.wav"
                _save_chunk_wav(audio, sr, start_sec, end_sec, chunk_path)
                print(
                    f"[transcribe] chunk {idx}/{len(plan)}"
                    f" [{format_timestamp(start_sec)}-{format_timestamp(end_sec)}]",
                    file=sys.stderr,
                )
                try:
                    results = self.model.transcribe(
                        str(chunk_path), context=vocab_context
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"ASR failed on chunk {idx}/{len(plan)} "
                        f"({format_timestamp(start_sec)}): {exc}"
                        f" / 第 {idx}/{len(plan)} 块转录失败"
                        f"（{format_timestamp(start_sec)}）：{exc}"
                    ) from exc

                # Collapse every ASRTranscription in this chunk into one
                # Segment tagged with the chunk's outer boundaries.
                texts: list[str] = []
                for r in results:
                    txt = getattr(r, "text", "")
                    if isinstance(txt, str):
                        txt = txt.strip()
                    if txt:
                        texts.append(txt)
                if texts:
                    segments.append(
                        Segment(
                            start=start_sec,
                            end=end_sec,
                            text=" ".join(texts),
                        )
                    )

        return segments
