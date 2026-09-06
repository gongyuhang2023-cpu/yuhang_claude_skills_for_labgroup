"""Per-process audio recording via the Windows Process Loopback API.

The recorder targets a single PID and writes the captured stream to a WAV file.
System volume changes and mute do NOT affect the recording (validated in the
project spike — see SPEC §1 success criterion S1).

ProcTap delivers PCM as 48 kHz / 2 ch / float32 by default. We convert each
sample to 16-bit signed PCM on stop and write a standard WAV container.

Public API:
  - AudioRecorder(pid, output_path): per-process recorder, also usable as a
    context manager (start on __enter__, stop on __exit__).
  - SystemAudioRecorder(output_path): system-loopback recorder (the default
    output endpoint's mix), same interface — the fallback for apps where
    per-process loopback records silence (notably new Teams / WebView2).
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
import sys
import threading
from pathlib import Path
from types import TracebackType

import numpy as np
from proctap import ProcessAudioCapture

from .wavsink import FeedGate, WavSink

SAMPLE_RATE: int = 48000
CHANNELS: int = 2
SAMPLE_WIDTH_BYTES: int = 2  # int16 output

# System-loopback capture granularity (soundcard's blocking pull size).
# 4800 frames = 0.1 s @ 48 kHz — small enough that stop() reacts within ~0.1 s.
_SYS_LOOPBACK_BLOCK: int = 4800


# The float32→int16 conversion moved to wavsink.py, which now owns writing.
# Both recorders here and MicRecorder go through WavSink, so nothing in this
# module needs it any more.


def _merge_gate_stats(sink: WavSink | None, gate: FeedGate) -> dict[str, object]:
    """Sink counters plus the gate's late-arrival tally.

    Read this rather than `stop_errors` when you want the final numbers: late
    callbacks keep trickling in after `stop()` has returned, so anything
    computed during stop is a snapshot (see `_sink_diagnostics`).
    """
    stats: dict[str, object] = dict(sink.stats()) if sink is not None else {}
    stats["late_calls"] = gate.late_calls
    stats["late_bytes"] = gate.late_bytes
    return stats


def _settle_pending_sink(
    sink: WavSink | None, gate: FeedGate, stop_errors: list[str]
) -> None:
    """Give a sink whose earlier `finish()` timed out another chance.

    `WavSink` deliberately stays retryable after a timeout, but the producer
    reference is already gone by then, so a second `stop()` would otherwise
    return early and never try again — leaving the sink permanently unsettled
    even though the writer had since freed up. Called from every recorder's
    early-return path so "stop it again" actually means something.
    """
    if sink is None or sink.finished:
        return
    sink.finish()
    if sink.finished:
        stop_errors.extend(_sink_diagnostics(sink, gate))


def _sink_diagnostics(sink: WavSink, gate: FeedGate) -> list[str]:
    """Turn writer- and gate-side losses into `stop_errors` lines.

    These conditions used to be unobservable: audio that never reached the
    file simply was not there, with nothing to say so. Every one of them is
    now counted, and a count nobody reports is no better than no count.

    One caveat, measured rather than assumed: the late-callback line almost
    never fires. Only arrivals landing between `gate.close()` and this call
    can be seen, and that window is microseconds, while a stalled producer
    delivers over the following second or so. The line stays because it is
    right when it does fire, but **`sink_stats` is the read that actually sees
    late arrivals** — it reports live counters, so metadata and the closing
    banner get the real numbers. Waiting here for a thread that may never
    finish is not an option, which is the whole reason the gate exists.
    """
    problems: list[str] = []
    if sink.writer_error is not None:
        problems.append(f"audio writer failed: {sink.writer_error}")
    if sink.dropped_bytes:
        seconds = sink.dropped_bytes / (
            sink.samplerate * sink.channels * 4
        )
        problems.append(
            f"dropped {sink.dropped_bytes} bytes (~{seconds:.1f}s) of audio: "
            f"the disk could not keep up"
        )
    if gate.late_bytes:
        seconds = gate.late_bytes / (sink.samplerate * sink.channels * 4)
        problems.append(
            f"{gate.late_calls} capture callback(s) arrived after stop "
            f"(~{seconds:.2f}s discarded)"
        )
    if sink.size_limit_reached:
        problems.append(
            "recording hit the 4 GiB WAV limit (~6.2h) and stopped there"
        )
    return problems


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
        self._bytes_captured: int = 0
        self._tap: ProcessAudioCapture | None = None
        self._stop_errors: list[str] = []
        self._has_nonzero: bool = False
        self._sink: WavSink | None = None
        self._gate = FeedGate()

    # ------------------------------------------------------------------ props

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def bytes_captured(self) -> int:
        """Float32 bytes delivered by ProcTap — a capture-side diagnostic."""
        return self._bytes_captured

    @property
    def duration_seconds(self) -> float:
        """Seconds of audio actually written to the WAV.

        Measured at the file, not at the device: bytes that never reached disk
        are not audio anyone can play, and `metadata.json` must not claim them.
        """
        return self._sink.written_seconds if self._sink is not None else 0.0

    @property
    def has_signal(self) -> bool:
        """True once any non-zero audio sample has been received.

        Kept on the capture side because `session.py` reads it ten seconds into
        the recording, while the sink's accumulators lag behind the writer.
        """
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
        if self._tap is not None:
            raise RuntimeError("AudioRecorder already started")

        # File first, device second — see SystemAudioRecorder.start().
        sink = WavSink(self._output_path, SAMPLE_RATE, CHANNELS, name="audio")
        sink.start()
        self._sink = sink

        tap = ProcessAudioCapture(pid=self._pid, on_data=self._on_chunk)
        try:
            tap.start()
        except BaseException:
            # Best-effort close on partial init; do not mask the original error.
            with contextlib.suppress(Exception):
                tap.close()
            self._gate.close(timeout=2.0)
            sink.abort()
            self._sink = None
            raise
        self._tap = tap

    def stop(self) -> Path:
        """Close the gate, stop the tap, drain the sink. Idempotent.

        The gate closes **first** here, unlike `SystemAudioRecorder`. ProcTap's
        `stop()` joins its worker with a one-second timeout and then drops the
        thread reference whether or not it exited (see
        `vendor/proctap/src/proctap/core.py`), so there is no way to wait for
        that thread to be gone — `close()` cannot even retry the join.

        Closing the gate first therefore trades a small, measurable loss (up to
        the last block or two, ~200 ms) for a definite cut-off, instead of an
        indefinite and unmeasurable race. Anything the worker delivers after
        this point is counted in `stop_errors` rather than silently vanishing.
        """
        if self._tap is None:
            # Never started, or already stopped — but an earlier stop() may
            # have timed out with the writer still holding the file, so give
            # the sink another chance to settle before returning.
            _settle_pending_sink(self._sink, self._gate, self._stop_errors)
            return self._output_path

        if not self._gate.close(timeout=5.0):
            self._stop_errors.append("feed gate did not close within 5s")

        tap, self._tap = self._tap, None

        for op_name, op in (("stop", tap.stop), ("close", tap.close)):
            try:
                op()
            except Exception as exc:  # noqa: BLE001 — proctap raises broadly
                self._stop_errors.append(f"{op_name}: {exc!r}")

        if self._sink is not None:
            self._sink.finish()
            self._stop_errors.extend(_sink_diagnostics(self._sink, self._gate))
            self._warn_if_silent()

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
        # ProcTap hands over a bytes object it does not reuse, so no copy is
        # needed here — but nothing slow may go in either: ProcTap's worker
        # swallows exceptions from this callback and any delay here backs up
        # its queue.
        self._bytes_captured += len(pcm)
        if not self._has_nonzero and any(pcm):
            self._has_nonzero = True
        if self._gate.enter(len(pcm)):
            try:
                sink = self._sink
                if sink is not None:
                    sink.feed(pcm)
            finally:
                self._gate.leave()

    def _warn_if_silent(self) -> None:
        """Report a recording that captured frames but no signal.

        Process loopback has its own failure modes (notably new Teams, whose
        audio is rendered outside the target process tree), so the causes
        listed here differ from the system-loopback ones.
        """
        sink = self._sink
        if sink is None or not sink.is_silent or sink.written_frames == 0:
            return
        print(
            f"\n⚠️  WARNING: Audio is SILENT (RMS={sink.rms:.2e}). "
            f"The recording captured {sink.written_frames} frames "
            f"but all samples are zero. Possible causes:\n"
            f"  - WASAPI process loopback bug (check proctap patch)\n"
            f"  - Meeting software was not playing audio\n"
            f"  - Wrong process targeted\n",
            file=sys.stderr,
            flush=True,
        )


class SystemAudioRecorder:
    """Record the default output endpoint's system loopback to a WAV file.

    Where `AudioRecorder` targets a single process via WASAPI *process* loopback,
    this captures the whole default render endpoint's mix — whatever the speakers
    play — via WASAPI *system* loopback (through the `soundcard` library). It does
    not depend on process attribution, so it still works when process loopback
    delivers only silence: notably new Teams / WebView2, whose meeting audio is
    rendered by a Chromium audio-service process that escapes the target process
    tree. Trade-off: unrelated system sounds (notifications, other apps) are
    captured too.

    Same public surface as `AudioRecorder` (start/stop, duration_seconds,
    has_signal, is_silent, bytes_captured, stop_errors, output_path, context
    manager) and an identical output format (48 kHz / 2 ch / int16 WAV), so
    `MeetingSession` drives both with the same idioms.

    soundcard exposes a *pull* API (blocking `recorder.record(numframes)`), not a
    callback, so a background thread owns the recorder for its whole lifetime —
    opened, pumped, and closed on that one thread (WASAPI/COM objects are
    thread-affine). `start()` blocks until the stream is open (or startup fails);
    teardown mirrors `AudioRecorder`: idempotent, never raises, never loses the
    recording, and surfaces teardown issues via `stop_errors`.
    """

    def __init__(self, output_path: Path, device_name: str | None = None) -> None:
        self._output_path = Path(output_path)
        self._device_name = device_name  # None → default output endpoint
        self._bytes_captured: int = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._run_error: BaseException | None = None
        self._stop_errors: list[str] = []
        self._has_nonzero: bool = False
        self._device_label: str | None = None
        self._sink: WavSink | None = None
        self._gate = FeedGate()

    # ------------------------------------------------------------------ props

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def bytes_captured(self) -> int:
        """Float32 bytes handed over by the device — a capture-side diagnostic.

        Not the same as what is on disk: see `duration_seconds`.
        """
        return self._bytes_captured

    @property
    def duration_seconds(self) -> float:
        """Seconds of audio actually written to the WAV.

        Deliberately measured at the file rather than at the device. Bytes that
        were captured but never made it to disk (writer failure, shed under a
        stalled disk) are not audio anyone can play, and `metadata.json` must
        not claim them.
        """
        return self._sink.written_seconds if self._sink is not None else 0.0

    @property
    def has_signal(self) -> bool:
        """True once any non-zero audio sample has been received.

        Read *during* recording (the 10-second self-check), so it stays on the
        capture side rather than deferring to the sink, whose accumulators are
        updated by the writer thread and therefore lag.
        """
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
    def device_label(self) -> str | None:
        """Name of the loopback device actually captured (set after start())."""
        return self._device_label

    @property
    def sink_stats(self) -> dict[str, object]:
        """Writer- and gate-side counters, read live.

        Prefer this over `stop_errors` for final numbers: late callbacks keep
        arriving after stop() returns, so the stop-time snapshot undercounts.
        """
        return _merge_gate_stats(self._sink, self._gate)

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("SystemAudioRecorder already started")

        # Open the file first: creating it is cheap and predictable, whereas
        # opening a WASAPI endpoint is neither. If the device then fails we
        # roll the file back with one unlink, and the caller is left with no
        # trace — which `MeetingSession` relies on when it undoes a failed start.
        sink = WavSink(self._output_path, SAMPLE_RATE, CHANNELS, name="audio")
        sink.start()
        self._sink = sink

        self._stop_event.clear()
        self._ready.clear()
        self._start_error = None
        thread = threading.Thread(
            target=self._run, name="meetingmind-syscap", daemon=True
        )
        try:
            thread.start()

            # Block until the capture stream is open (or startup failed) so
            # callers get the same fail-fast semantics as AudioRecorder.start().
            if not self._ready.wait(timeout=10.0):
                self._stop_event.set()
                thread.join(timeout=2.0)
                raise RuntimeError(
                    "system audio capture did not start within 10s "
                    "(no WASAPI loopback data from the default output device)"
                )
            if self._start_error is not None:
                thread.join(timeout=2.0)
                raise self._start_error
        except BaseException:
            self._gate.close(timeout=2.0)
            sink.abort()
            self._sink = None
            raise
        self._thread = thread

    def _run(self) -> None:
        """Background thread: open the loopback stream, pump until stopped."""
        try:
            mic = self._resolve_loopback()
            self._device_label = getattr(mic, "name", None)
            with mic.recorder(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                blocksize=_SYS_LOOPBACK_BLOCK,
            ) as rec:
                self._ready.set()  # stream open → start() may return
                while not self._stop_event.is_set():
                    chunk = rec.record(numframes=_SYS_LOOPBACK_BLOCK)
                    if chunk is None or len(chunk) == 0:
                        continue
                    buf = np.ascontiguousarray(chunk, dtype="<f4").tobytes()
                    self._bytes_captured += len(buf)
                    if not self._has_nonzero and bool(np.any(chunk)):
                        self._has_nonzero = True
                    # Hand off and get straight back to rec.record(). Anything
                    # slow here lets the WASAPI ring buffer wrap, which loses
                    # audio at the device where nothing can recover it.
                    if self._gate.enter(len(buf)):
                        try:
                            sink = self._sink
                            if sink is not None:
                                sink.feed(buf)
                        finally:
                            self._gate.leave()
        except BaseException as exc:  # noqa: BLE001 — thread must not crash silently
            # Before the stream opened → a startup failure (start() re-raises it);
            # after → a mid-recording failure (surfaced via stop_errors).
            if self._ready.is_set():
                self._run_error = exc
            else:
                self._start_error = exc
        finally:
            self._ready.set()

    def stop(self) -> Path:
        """Stop the capture thread, drain the sink, close the WAV.

        Returns the output path. Idempotent: safe to call repeatedly.

        Order matters. This recorder owns its capture thread, so joining first
        collects every block the device produced; the gate is then a formality
        that only earns its keep on the 15-second-timeout path, where a thread
        we could not join might still be delivering audio.

        (`AudioRecorder` inverts this — see the note on its own `stop`.)
        """
        if self._thread is None:
            # Never started, startup failed (no WAV owed), or already stopped —
            # in the last case an earlier finish() may have timed out, so retry.
            _settle_pending_sink(self._sink, self._gate, self._stop_errors)
            return self._output_path

        self._stop_event.set()
        thread, self._thread = self._thread, None
        thread.join(timeout=15.0)
        if thread.is_alive():
            self._stop_errors.append("capture thread did not stop within 15s")
        if self._run_error is not None:
            self._stop_errors.append(f"capture: {self._run_error!r}")

        if not self._gate.close(timeout=5.0):
            self._stop_errors.append("feed gate did not close within 5s")

        if self._sink is not None:
            self._sink.finish()
            self._stop_errors.extend(_sink_diagnostics(self._sink, self._gate))
            self._warn_if_silent()

        return self._output_path

    # ----------------------------------------------------------------- context

    def __enter__(self) -> SystemAudioRecorder:
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

    def _resolve_loopback(self):  # type: ignore[no-untyped-def]  # soundcard mic
        """Pick the loopback capture device: the named one, else the default
        output endpoint's loopback, else the first available loopback device."""
        import soundcard as sc  # noqa: PLC0415 — lazy: only when system audio is on

        if self._device_name:
            mic = sc.get_microphone(self._device_name, include_loopback=True)
            if mic is None:
                raise RuntimeError(f"loopback device not found: {self._device_name!r}")
            return mic

        speaker = sc.default_speaker()
        try:
            mic = sc.get_microphone(speaker.name, include_loopback=True)
        except Exception:  # noqa: BLE001 — fall through to scanning loopbacks
            mic = None
        if mic is None or not getattr(mic, "isloopback", False):
            loopbacks = [
                m
                for m in sc.all_microphones(include_loopback=True)
                if getattr(m, "isloopback", False)
            ]
            if not loopbacks:
                raise RuntimeError(
                    "no WASAPI loopback device available for system-audio capture"
                )
            mic = loopbacks[0]
        return mic

    def _warn_if_silent(self) -> None:
        """Report a recording that captured frames but no signal.

        Worth keeping separate from the generic session-level warning: the
        causes here are specific to system loopback, and knowing them is the
        difference between re-recording correctly and re-recording silence.
        """
        sink = self._sink
        if sink is None or not sink.is_silent or sink.written_frames == 0:
            return
        print(
            f"\n⚠️  WARNING: System audio is SILENT (RMS={sink.rms:.2e}). "
            f"Captured {sink.written_frames} frames but all samples are "
            f"zero. Possible causes:\n"
            f"  - Nothing was playing on the default output device\n"
            f"  - Meeting audio is routed to a NON-default output device "
            f"(set the meeting's speaker to the Windows default, re-record)\n"
            f"  - Default output device changed mid-recording\n",
            file=sys.stderr,
            flush=True,
        )
