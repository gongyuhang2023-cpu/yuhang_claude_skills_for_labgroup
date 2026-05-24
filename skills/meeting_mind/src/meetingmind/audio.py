"""Per-process audio recording via the Windows Process Loopback API.

The recorder targets a single PID and writes the captured stream to a WAV file.
System volume changes and mute do NOT affect the recording (validated in the
project spike — see SPEC §1 success criterion S1).

ProcTap delivers PCM as 48 kHz / 2 ch / float32 by default. We convert each
sample to 16-bit signed PCM on stop and write a standard WAV container.

Public API:
  - AudioRecorder(pid, output_path): the recorder, also usable as a context
    manager (start on __enter__, stop on __exit__).
  - SAMPLE_RATE / CHANNELS / SAMPLE_WIDTH_BYTES: format constants.

Design choices worth noting:
  - `stop()` is idempotent and tries hard to flush whatever has been captured,
    even if ProcTap raises during teardown. Any teardown errors are surfaced
    via the `stop_errors` attribute, never by raising — recordings should
    never be lost because of a faulty close().
  - `start()` cleans up the half-initialized tap if ProcTap.start() throws,
    so the recorder is safe to retry or discard.
"""

from __future__ import annotations

import contextlib
import threading
import wave
from pathlib import Path
from types import TracebackType

import numpy as np
from proctap import ProcessAudioCapture

SAMPLE_RATE: int = 48000
CHANNELS: int = 2
SAMPLE_WIDTH_BYTES: int = 2  # int16 output

_BYTES_PER_FLOAT32_FRAME: int = 4 * CHANNELS  # 4 bytes per float, 2 channels


def _float32_to_int16(samples: np.ndarray) -> np.ndarray:
    """Convert float32 PCM in [-1, 1] to int16.

    Uses symmetric scaling (factor 32767) so ±1.0 maps to ±32767. This matches
    the spike-validated conversion and most audio toolchains; the small loss
    of one negative LSB is preferred over breaking symmetry around zero.
    """
    if samples.size == 0:
        return np.array([], dtype=np.int16)
    return np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16)


class AudioRecorder:
    """Record audio from a single process to a WAV file.

    Usage:
        rec = AudioRecorder(pid=49904, output_path=Path("out.wav"))
        rec.start()
        ...
        rec.stop()

    Or as a context manager:
        with AudioRecorder(49904, Path("out.wav")) as rec:
            ...
        # WAV is written on __exit__ even if the body raised.
    """

    def __init__(self, pid: int, output_path: Path) -> None:
        self._pid = pid
        self._output_path = Path(output_path)
        self._frames: list[bytes] = []
        self._bytes_captured: int = 0
        self._lock = threading.Lock()
        self._tap: ProcessAudioCapture | None = None
        self._stop_errors: list[str] = []
        self._wav_written: bool = False

    # ------------------------------------------------------------------ props

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def bytes_captured(self) -> int:
        return self._bytes_captured

    @property
    def duration_seconds(self) -> float:
        """Estimated seconds of audio captured (independent of WAV state)."""
        if self._bytes_captured == 0:
            return 0.0
        return self._bytes_captured / (SAMPLE_RATE * _BYTES_PER_FLOAT32_FRAME)

    @property
    def stop_errors(self) -> list[str]:
        """Diagnostics from a non-fatal failure during stop()."""
        return list(self._stop_errors)

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._tap is not None:
            raise RuntimeError("AudioRecorder already started")

        tap = ProcessAudioCapture(pid=self._pid, on_data=self._on_chunk)
        try:
            tap.start()
        except Exception:
            # Best-effort close on partial init; do not mask the original error.
            with contextlib.suppress(Exception):
                tap.close()
            raise
        self._tap = tap

    def stop(self) -> Path:
        """Stop recording and flush a WAV file.

        Returns the output path. Idempotent: safe to call repeatedly.
        If the underlying tap raises during teardown, the WAV is still written
        from whatever has been captured, and the error message is recorded in
        `stop_errors`.
        """
        if self._tap is None:
            # Either never started, or already stopped. Either way: nothing more to do.
            return self._output_path

        tap, self._tap = self._tap, None

        for op_name, op in (("stop", tap.stop), ("close", tap.close)):
            try:
                op()
            except Exception as exc:  # noqa: BLE001 — proctap raises broadly
                self._stop_errors.append(f"{op_name}: {exc!r}")

        self._write_wav()
        return self._output_path

    # ----------------------------------------------------------------- context

    def __enter__(self) -> AudioRecorder:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Return None (not True) so the context manager never suppresses exceptions.
        self.stop()

    # ----------------------------------------------------------------- internal

    def _on_chunk(self, pcm: bytes, frame_count: int) -> None:
        with self._lock:
            self._frames.append(pcm)
            self._bytes_captured += len(pcm)

    def _write_wav(self) -> None:
        if self._wav_written:
            return
        self._wav_written = True

        with self._lock:
            data = b"".join(self._frames)

        floats = np.frombuffer(data, dtype=np.float32)
        # Truncate to whole stereo frames in case proctap delivered an odd tail.
        usable = (floats.size // CHANNELS) * CHANNELS
        int16 = _float32_to_int16(floats[:usable])

        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self._output_path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH_BYTES)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(int16.tobytes())
