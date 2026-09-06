"""Default-microphone capture via sounddevice (PortAudio).

Companion to `AudioRecorder`. Where `AudioRecorder` captures a *process's*
render output (the people you are listening to) via WASAPI process loopback,
`MicRecorder` captures *your own voice* from the default input device.

The two are used together by the two-stream path (`record --mic`): a 1:1
meeting then yields one WAV per speaker — ``mic.wav`` (you) +
``recording.wav`` (the other side) — which transcribe + merge label by speaker
without any diarization model, because each stream already contains exactly
one voice.

The mic stream is captured mono at the device's native sample rate; the
transcriber resamples to 16 kHz on its own (librosa), so no rate negotiation
is needed here. Like `AudioRecorder`:
  - `stop()` is idempotent and never raises on teardown — a faulty close must
    not lose the recording; teardown errors surface via `stop_errors`.
  - `start()` cleans up a half-opened stream if sounddevice raises, so the
    recorder is safe to retry or discard.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import TracebackType
from typing import Any

import numpy as np

from .audio import _merge_gate_stats, _settle_pending_sink, _sink_diagnostics
from .wavsink import FeedGate, WavSink

# Fallback sample rate if the device refuses to report one (rare). 16 kHz is
# the ASR target, so a fallback file is still transcribable.
_FALLBACK_SAMPLE_RATE: int = 16000
CHANNELS: int = 1  # mic captured mono — one voice, smaller file, ASR wants mono


class MicRecorder:
    """Record the default input device (microphone) to a mono WAV file.

    Mirrors `AudioRecorder`'s lifecycle (start / stop / has_signal /
    duration_seconds) so `MeetingSession` can drive both with the same idioms.

    Usage:
        rec = MicRecorder(output_path=Path("mic.wav"))
        rec.start()
        ...
        rec.stop()
    """

    def __init__(
        self,
        output_path: Path,
        device: int | None = None,
        samplerate: int | None = None,
    ) -> None:
        self._output_path = Path(output_path)
        self._device = device
        self._samplerate = samplerate  # resolved from the device at start() if None
        self._frames_captured: int = 0
        self._stream: Any = None  # sounddevice.InputStream
        self._stop_errors: list[str] = []
        self._has_nonzero: bool = False
        self._sink: WavSink | None = None
        self._gate = FeedGate()

    # ------------------------------------------------------------------ props

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def samplerate(self) -> int | None:
        return self._samplerate

    @property
    def duration_seconds(self) -> float:
        """Seconds of audio actually written to the WAV (see `AudioRecorder`)."""
        return self._sink.written_seconds if self._sink is not None else 0.0

    @property
    def has_signal(self) -> bool:
        """True once any non-zero audio sample has been received."""
        return self._has_nonzero

    @property
    def is_silent(self) -> bool:
        """True if the captured audio is entirely silent — valid after stop()."""
        return self._sink.is_silent if self._sink is not None else False

    @property
    def stop_errors(self) -> list[str]:
        """Diagnostics from a non-fatal failure during stop()."""
        return list(self._stop_errors)

    @property
    def sink_stats(self) -> dict[str, object]:
        """Writer- and gate-side counters, read live.

        Prefer this over `stop_errors` for final numbers: late callbacks keep
        arriving after stop() returns, so the stop-time snapshot undercounts.
        """
        return _merge_gate_stats(self._sink, self._gate)

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._stream is not None:
            raise RuntimeError("MicRecorder already started")

        # Lazy import: only pay the sounddevice/PortAudio import when --mic is on.
        import sounddevice as sd  # noqa: PLC0415 — deliberate lazy import

        # The sample rate must be known before the sink exists — it goes into
        # the WAV header, which is written up front rather than at the end.
        # This is the one structural constraint the mic path adds.
        if self._samplerate is None:
            self._samplerate = self._resolve_samplerate(sd)

        sink = WavSink(self._output_path, self._samplerate, CHANNELS, name="mic")
        sink.start()
        self._sink = sink

        stream = sd.InputStream(
            samplerate=self._samplerate,
            channels=CHANNELS,
            device=self._device,  # None → system default input device
            dtype="float32",
            callback=self._on_audio,
        )
        try:
            stream.start()
        except BaseException:
            with contextlib.suppress(Exception):
                stream.close()
            self._gate.close(timeout=2.0)
            sink.abort()
            self._sink = None
            raise
        self._stream = stream

    def _resolve_samplerate(self, sd: Any) -> int:
        """Query the device's native sample rate; fall back to 16 kHz on error."""
        try:
            if self._device is not None:
                info = sd.query_devices(self._device, kind="input")
            else:
                info = sd.query_devices(kind="input")
            rate = int(info["default_samplerate"])
            return rate if rate > 0 else _FALLBACK_SAMPLE_RATE
        except Exception:  # noqa: BLE001 — any probe failure → safe fallback
            return _FALLBACK_SAMPLE_RATE

    def stop(self) -> Path:
        """Close the gate, stop the stream, drain the sink. Idempotent.

        The gate closes *first*, as with `AudioRecorder`: PortAudio owns the
        callback thread, so there is nothing to join and the only way to get a
        definite cut-off is to draw it ourselves. Teardown errors surface via
        `stop_errors` and never raise — a faulty close must not cost the
        recording.
        """
        if self._stream is None:
            # An earlier stop() may have timed out with the writer still busy;
            # retry settling the sink rather than returning a lie.
            _settle_pending_sink(self._sink, self._gate, self._stop_errors)
            return self._output_path

        if not self._gate.close(timeout=5.0):
            self._stop_errors.append("mic feed gate did not close within 5s")

        stream, self._stream = self._stream, None
        for op_name, op in (("stop", stream.stop), ("close", stream.close)):
            try:
                op()
            except Exception as exc:  # noqa: BLE001 — sounddevice raises broadly
                self._stop_errors.append(f"{op_name}: {exc!r}")

        if self._sink is not None:
            self._sink.finish()
            self._stop_errors.extend(_sink_diagnostics(self._sink, self._gate))

        return self._output_path

    # ----------------------------------------------------------------- context

    def __enter__(self) -> MicRecorder:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # ----------------------------------------------------------------- internal

    def _on_audio(
        self, indata: np.ndarray, frames: int, time_info: Any, status: Any
    ) -> None:
        # This runs on PortAudio's real-time callback thread. `indata` is
        # (frames, channels) float32 owned by PortAudio and is invalid the
        # moment this returns, so the copy has to happen here — handing the
        # writer thread a view would give it freed memory. It is also why
        # nothing slow may go in this function: blocking the callback drops
        # audio at the device.
        buf = np.ascontiguousarray(indata[:, 0], dtype="<f4").tobytes()
        self._frames_captured += int(frames)
        if not self._has_nonzero and bool(np.any(indata[:, 0])):
            self._has_nonzero = True
        if self._gate.enter(len(buf)):
            try:
                sink = self._sink
                if sink is not None:
                    sink.feed(buf)
            finally:
                self._gate.leave()
