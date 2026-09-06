"""Incremental WAV writer: audio reaches disk while the meeting is still running.

Why this module exists
----------------------
The original recorders held every captured chunk in a Python list and only
turned it into a file inside ``stop()``. Two consequences, both bad:

1. Any abnormal exit lost *everything*. On 2026-08-03 a 115-minute Teams
   recording vanished because the process died during that final write —
   the screenshots survived (they are saved one-by-one) but ``recording.wav``
   had never existed.
2. That final write allocated several full-length copies of the recording
   (join, square-for-RMS, scale, clip, cast, tobytes). At 48 kHz stereo
   float32 the capture buffer alone grows 1.38 GB/hour, and the write peaked
   around 12 GB for that meeting. Short meetings never exposed it.

``WavSink`` fixes both: a writer thread drains a bounded queue and appends to
the file continuously, so the recording on disk is always current, and memory
stays bounded no matter how long the meeting runs.

The load-bearing invariant
--------------------------
**``feed()`` is safe to call at any moment** — before ``start()``, after
``finish()``, after the file is closed, after the writer thread has died, while
the interpreter is shutting down. It only ever updates counters and, at worst,
drops the chunk. It never touches the file handle, never blocks, never raises.

That is what makes late capture callbacks harmless. The vendored ProcTap's
``stop()`` joins its worker with a 1-second timeout and then drops the thread
reference unconditionally (``vendor/proctap/src/proctap/core.py``) — that
timeout is "stop waiting", *not* "the thread is now dead". No amount of
grace-period waiting can make that safe, so we do not try: we make arrival
order irrelevant instead.

``feed()`` never blocking is also what lets :class:`FeedGate` (added alongside
the adapters) bound its own wait. Do not add logging, allocation-heavy work, or
any I/O to ``feed()`` — logging takes locks and can itself write files, which
would turn that bounded wait into a potential deadlock.

Why not ``wave.Wave_write``
---------------------------
``Wave_write.close()`` unconditionally patches the header from its own byte
counter, and ``__del__`` calls ``close()`` again at GC time, raising into the
void. That collides head-on with this module's error policy: when a write
fails we want the on-disk length fields left exactly as they were, describing
the last known-good state. Owning the 44 bytes ourselves is ~30 lines, makes
the layout a tested contract rather than an assumption about the stdlib, and
puts the patch cadence and the 4 GiB ceiling under our control. The header
bytes are produced by the same struct expression ``wave`` uses, so the two are
directly comparable in tests.
"""

from __future__ import annotations

import atexit
import contextlib
import struct
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

# ---------------------------------------------------------------- constants

_WAV_HEADER_BYTES: int = 44
_WAVE_FORMAT_PCM: int = 0x0001
_OUTPUT_SAMPLE_WIDTH: int = 2  # int16 on disk
_INPUT_SAMPLE_WIDTH: int = 4  # float32 on the wire

# `36 + data_length` must fit in the RIFF header's unsigned 32-bit field.
# At 48 kHz stereo int16 this caps a recording at roughly 6.2 hours.
_MAX_DATA_BYTES: int = 0xFFFFFFFF - 36

# Matches the threshold the recorders have always used for "completely silent".
_SILENCE_RMS_THRESHOLD: float = 1e-7

_DEFAULT_MAX_BUFFER_SECONDS: float = 60.0
_DEFAULT_BATCH_BYTES: int = 4 << 20

# Live sinks, so the atexit hook below can flush them if the main flow exits
# without calling stop().
_LIVE_SINKS: weakref.WeakSet[WavSink] = weakref.WeakSet()


def _flush_live_sinks_at_exit() -> None:
    """Drain any still-open sink before the interpreter tears itself down.

    The writer is a daemon thread, deliberately: a bug in it must not be able
    to hang the process. The cost of that choice is that Python kills daemon
    threads at shutdown wherever they happen to be, so a `record` run that
    exits through an unhandled exception — never reaching stop() — would lose
    whatever was still queued.

    `atexit` handlers run *before* daemon threads are killed, so this is the
    one place that gap can be closed. Everything already written is safe
    regardless; this recovers the last second or two.
    """
    for sink in list(_LIVE_SINKS):
        with contextlib.suppress(Exception):
            sink.finish(timeout=5.0)


atexit.register(_flush_live_sinks_at_exit)


def _float32_to_int16(samples: np.ndarray) -> np.ndarray:
    """Convert float32 PCM in [-1, 1] to int16.

    Uses symmetric scaling (factor 32767) so ±1.0 maps to ±32767. This matches
    the spike-validated conversion and most audio toolchains; the small loss
    of one negative LSB is preferred over breaking symmetry around zero.

    The expression is deliberately identical to the pre-refactor one in
    ``audio.py`` — new output must be byte-for-byte comparable with old output.
    """
    if samples.size == 0:
        return np.array([], dtype=np.int16)
    return np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16)


def _default_opener(path: Path) -> BinaryIO:
    return open(path, "wb")


# --------------------------------------------------------------- feed gate


class FeedGate:
    """A linearization barrier between capture callbacks and ``stop()``.

    What it is for
    --------------
    It is **not** a crash-safety mechanism — :meth:`WavSink.feed` being safe at
    any time already makes late callbacks harmless. The gate buys two other
    things:

    * a definite cut-off, so ``written_frames`` means something reproducible
      rather than "whatever raced in before the file closed";
    * a count of what arrived too late, turning an invisible loss into a number
      that can be reported.

    Consequently, **a timed-out close must not stop the caller from finishing**.
    If the gate cannot confirm the producer is idle, carry on and tear down
    anyway; the sink does not care.

    Why the wait is bounded
    -----------------------
    :meth:`close` waits for in-flight calls to leave. That is only safe because
    the work inside the gate — a single ``feed()`` — never blocks. Put anything
    that can block (I/O, logging, a lock someone else holds) between
    :meth:`enter` and :meth:`leave` and this becomes a deadlock waiting to
    happen.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._accepting = True
        self._active = 0
        self._late_calls = 0
        self._late_bytes = 0

    @property
    def late_calls(self) -> int:
        """Callbacks that arrived after the gate closed — i.e. after ``stop()``."""
        return self._late_calls

    @property
    def late_bytes(self) -> int:
        return self._late_bytes

    @property
    def accepting(self) -> bool:
        return self._accepting

    def enter(self, nbytes: int = 0) -> bool:
        """Claim passage. ``True`` means proceed and then call :meth:`leave`."""
        with self._lock:
            if not self._accepting:
                self._late_calls += 1
                self._late_bytes += nbytes
                return False
            self._active += 1
            return True

    def leave(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1
            if self._active == 0:
                self._idle.notify_all()

    def close(self, timeout: float = 5.0) -> bool:
        """Stop accepting, then wait for in-flight callers to leave.

        Returns whether the barrier was established cleanly. ``False`` means
        somebody is still inside after ``timeout`` — which, given the
        never-blocks rule above, means that rule has been broken. Report it;
        do not let it change the teardown path.

        Idempotent.
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            self._accepting = False
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
        return True


# -------------------------------------------------------------------- sink


class WavSink:
    """Append-only PCM WAV writer fed from one or more capture threads.

    Usage::

        sink = WavSink(path, samplerate=48000, channels=2)
        sink.start()          # synchronous; raises if the file cannot be made
        sink.feed(pcm_bytes)  # from the capture thread, as often as it likes
        sink.finish()         # drain, patch, close

    ``start()`` does every failure-prone thing (mkdir, open, header) on the
    calling thread before the writer exists, so a caller can roll back with
    :meth:`abort` and be sure nothing was left behind.
    """

    def __init__(
        self,
        output_path: Path,
        samplerate: int,
        channels: int,
        *,
        max_buffer_seconds: float = _DEFAULT_MAX_BUFFER_SECONDS,
        batch_bytes: int = _DEFAULT_BATCH_BYTES,
        max_data_bytes: int = _MAX_DATA_BYTES,
        name: str = "audio",
        opener: Callable[[Path], BinaryIO] | None = None,
    ) -> None:
        if samplerate <= 0:
            raise ValueError(f"samplerate must be positive, got {samplerate}")
        if channels <= 0:
            raise ValueError(f"channels must be positive, got {channels}")

        self._output_path = Path(output_path)
        self._samplerate = int(samplerate)
        self._channels = int(channels)
        self._name = name
        self._opener = opener or _default_opener

        self._in_block = self._channels * _INPUT_SAMPLE_WIDTH
        self._out_block = self._channels * _OUTPUT_SAMPLE_WIDTH
        self._batch_bytes = int(batch_bytes)
        self._max_data_bytes = int(max_data_bytes)
        # Bound the queue in BYTES, not chunks: block sizes are chosen by the
        # audio device, so an element-count cap gives no real memory bound.
        self._max_queued_bytes = int(
            max_buffer_seconds * self._samplerate * self._in_block
        )

        self._cv = threading.Condition(threading.Lock())
        self._queue: deque[bytes] = deque()
        self._queued_bytes = 0
        self._closed = False

        self._fh: BinaryIO | None = None
        self._writer: threading.Thread | None = None
        self._started = False
        self._finished = False
        self._finish_lock = threading.Lock()

        # Written == on disk and covered by the patched header.
        self._written_bytes = 0
        self._written_frames = 0
        # Accepted == taken by feed(); may still be queued, or lost if the
        # writer dies. Never use this for audio_duration_seconds.
        self._accepted_bytes = 0
        self._dropped_chunks = 0
        self._dropped_bytes = 0
        self._first_drop_at_frame: int | None = None
        self._fed_after_close_bytes = 0
        self._unaligned_chunks = 0
        self._feed_errors = 0
        self._peak_queued_bytes = 0
        self._residue_bytes = 0

        self._sumsq = 0.0
        self._nsamples = 0
        self._rms = 0.0
        self._is_silent = False
        self._writer_error: str | None = None
        self._size_limit_reached = False
        self._finish_timed_out = False

    # ----------------------------------------------------------- properties

    @property
    def path(self) -> Path:
        return self._output_path

    @property
    def samplerate(self) -> int:
        return self._samplerate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def written_frames(self) -> int:
        return self._written_frames

    @property
    def written_bytes(self) -> int:
        return self._written_bytes

    @property
    def written_seconds(self) -> float:
        """Seconds of audio that are actually on disk and declared by the header.

        This is the only duration that may be reported as the recording's
        length. Accepted-but-unwritten bytes are not audio anyone can play.
        """
        return self._written_frames / float(self._samplerate)

    @property
    def dropped_bytes(self) -> int:
        return self._dropped_bytes

    @property
    def first_drop_at_frame(self) -> int | None:
        """Frame index where shedding began — where a listener would hear a gap."""
        return self._first_drop_at_frame

    @property
    def residue_bytes(self) -> int:
        """Bytes of a trailing partial frame discarded at finish() (< one frame)."""
        return self._residue_bytes

    @property
    def fed_after_close_bytes(self) -> int:
        return self._fed_after_close_bytes

    @property
    def peak_queued_bytes(self) -> int:
        return self._peak_queued_bytes

    @property
    def writer_error(self) -> str | None:
        return self._writer_error

    @property
    def size_limit_reached(self) -> bool:
        return self._size_limit_reached

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def finish_timed_out(self) -> bool:
        """True if a `finish()` gave up waiting and the writer is still going.

        The sink is *not* finished in that case — retrying `finish()` (which
        the atexit flush does) can still complete it.
        """
        return self._finish_timed_out

    @property
    def rms(self) -> float:
        return self._rms

    @property
    def is_silent(self) -> bool:
        """Whether the recording is entirely silent — only meaningful after finish().

        Before ``finish()`` this is ``False``, preserving the behaviour callers
        already rely on (the old recorders computed silence inside their final
        write, so reading it early always said "not silent").
        """
        return self._is_silent if self._finished else False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Create the file, write the header, start the writer thread.

        Everything that can fail happens here, on the caller's thread, so
        failures surface synchronously and leave no file behind.
        """
        if self._started:
            raise RuntimeError("WavSink already started")

        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._opener(self._output_path)
        try:
            self._write_header(fh, 0)
            fh.flush()
        except BaseException:
            with contextlib.suppress(Exception):
                fh.close()
            with contextlib.suppress(OSError):
                self._output_path.unlink()
            raise

        self._fh = fh
        self._started = True
        self._writer = threading.Thread(
            target=self._writer_loop, name=f"wavsink-{self._name}", daemon=True
        )
        self._writer.start()
        _LIVE_SINKS.add(self)

    def feed(self, pcm_f32le: bytes) -> None:
        """Hand over interleaved little-endian float32 PCM. Never blocks or raises.

        The caller gives up ownership of ``pcm_f32le`` and must not mutate it
        afterwards — the writer thread reads it later. Callers wrapping a
        library-owned buffer (PortAudio's ``indata``, for instance) must copy
        before calling.

        Safe at any point in the lifecycle; see the module docstring.
        """
        try:
            n = len(pcm_f32le)
            if n == 0:
                return
            with self._cv:
                if self._closed:
                    self._fed_after_close_bytes += n
                    return
                if self._queued_bytes + n > self._max_queued_bytes:
                    # Only reachable if the writer died or the disk stalled for
                    # a full buffer's worth of time. Drop the newest chunk:
                    # both choices leave a hole, and this one does not discard
                    # audio that was already safely queued.
                    self._dropped_chunks += 1
                    self._dropped_bytes += n
                    if self._first_drop_at_frame is None:
                        self._first_drop_at_frame = self._written_frames
                    return
                self._queue.append(pcm_f32le)
                self._queued_bytes += n
                self._accepted_bytes += n
                if n % _INPUT_SAMPLE_WIDTH:
                    self._unaligned_chunks += 1
                if self._queued_bytes > self._peak_queued_bytes:
                    self._peak_queued_bytes = self._queued_bytes
                self._cv.notify()
        except BaseException:  # noqa: BLE001 — the no-raise contract is the point
            self._feed_errors += 1

    def finish(self, timeout: float = 30.0) -> None:
        """Stop accepting, drain what is queued, close the file. Idempotent.

        Callers must already have stopped their capture source (and closed
        their :class:`FeedGate`, if any) — anything arriving after this is
        counted as ``fed_after_close_bytes`` rather than written.

        The ``timeout`` carries no correctness weight. It is not "wait this
        long, then assume the writer is dead and take the file" — on timeout we
        record an error and leave the file to the writer thread, which still
        owns it.
        """
        with self._finish_lock:
            if self._finished:
                return

            with self._cv:
                self._closed = True
                self._cv.notify_all()

            writer = self._writer
            if writer is not None:
                writer.join(timeout)
                if writer.is_alive():
                    if self._writer_error is None:
                        self._writer_error = (
                            f"writer thread did not finish within {timeout}s"
                        )
                    # Deliberately NOT marked finished. The writer still owns
                    # the file, the RMS has not been computed, and the sink
                    # stays in _LIVE_SINKS — so a later finish() and the atexit
                    # flush can both still complete this. Declaring it finished
                    # here would freeze the half-done state permanently and
                    # make `is_silent` look authoritative when it is a default.
                    self._finish_timed_out = True
                    return
                self._writer = None
                self._finish_timed_out = False

            # join() gives happens-before on everything the writer touched.
            if self._nsamples:
                self._rms = (self._sumsq / self._nsamples) ** 0.5
            else:
                self._rms = 0.0
            self._is_silent = self._rms < _SILENCE_RMS_THRESHOLD
            self._finished = True

        _LIVE_SINKS.discard(self)

    def abort(self) -> None:
        """Finish and delete the file — for rolling back a failed ``start()``.

        Without the unlink, a recorder whose device failed to open would leave
        a 44-byte empty WAV behind, and downstream tooling keys off that file
        existing.
        """
        self.finish(timeout=5.0)
        with contextlib.suppress(OSError):
            self._output_path.unlink()

    def stats(self) -> dict[str, Any]:
        """Everything worth putting in metadata or a diagnostic banner."""
        return {
            "path": str(self._output_path),
            "samplerate": self._samplerate,
            "channels": self._channels,
            "written_frames": self._written_frames,
            "written_bytes": self._written_bytes,
            "written_seconds": round(self.written_seconds, 3),
            "accepted_bytes": self._accepted_bytes,
            "dropped_chunks": self._dropped_chunks,
            "dropped_bytes": self._dropped_bytes,
            "first_drop_at_frame": self._first_drop_at_frame,
            "fed_after_close_bytes": self._fed_after_close_bytes,
            "unaligned_chunks": self._unaligned_chunks,
            "feed_errors": self._feed_errors,
            "residue_bytes": self._residue_bytes,
            "queued_bytes": self._queued_bytes,
            "peak_queued_bytes": self._peak_queued_bytes,
            "max_queued_bytes": self._max_queued_bytes,
            "rms": self._rms,
            "is_silent": self.is_silent,
            "writer_error": self._writer_error,
            "size_limit_reached": self._size_limit_reached,
            "finished": self._finished,
            "finish_timed_out": self._finish_timed_out,
        }

    # ------------------------------------------------------------- internal

    def _write_all(self, fh: BinaryIO, data: bytes) -> None:
        """Write every byte or raise. The only write path in this module.

        A file object may store fewer bytes than asked and say so in its return
        value rather than raising. Every write here — header, payload, length
        patches — has to check, because any of them silently half-completing
        produces a file whose header describes bytes that are not in it.

        `write` returns None on some raw streams, which asserts nothing; short
        of reading back there is no way to check those, so they are taken at
        their word. That is the one residual hole in "never over-report".
        """
        written = fh.write(data)
        if written is not None and written != len(data):
            raise OSError(
                f"short write: only {written} of {len(data)} bytes reached "
                f"{self._output_path}"
            )

    def _write_header(self, fh: BinaryIO, data_length: int) -> None:
        """Write the canonical 44-byte PCM header.

        Same struct expression as ``wave.Wave_write._write_header`` so the
        bytes are directly comparable with a stdlib-produced file.
        """
        self._write_all(fh, b"RIFF")
        self._write_all(
            fh,
            struct.pack(
                "<L4s4sLHHLLHH4s",
                36 + data_length,
                b"WAVE",
                b"fmt ",
                16,
                _WAVE_FORMAT_PCM,
                self._channels,
                self._samplerate,
                self._channels * self._samplerate * _OUTPUT_SAMPLE_WIDTH,
                self._channels * _OUTPUT_SAMPLE_WIDTH,
                _OUTPUT_SAMPLE_WIDTH * 8,
                b"data",
            ),
        )
        self._write_all(fh, struct.pack("<L", data_length))

    def _patch_lengths(self, fh: BinaryIO) -> None:
        """Update the two length fields to cover everything already flushed.

        Data length first, RIFF length second, and both only after the audio
        bytes themselves are flushed. Every field only ever grows, so any
        interrupted state under-reports rather than claiming bytes that are not
        there — and ``wave.Wave_read`` bounds the data chunk by the data length
        alone, so a stale RIFF length still reads back correctly.
        """
        pos = fh.tell()
        fh.seek(_WAV_HEADER_BYTES - 4)
        self._write_all(fh, struct.pack("<L", self._written_bytes))
        fh.seek(4)
        self._write_all(fh, struct.pack("<L", 36 + self._written_bytes))
        fh.seek(pos)
        fh.flush()

    def _writer_loop(self) -> None:
        residue = b""
        try:
            while True:
                with self._cv:
                    while not self._queue and not self._closed:
                        self._cv.wait()
                    if not self._queue and self._closed:
                        break
                    batch: list[bytes] = []
                    taken = 0
                    while self._queue and taken < self._batch_bytes:
                        chunk = self._queue.popleft()
                        batch.append(chunk)
                        taken += len(chunk)
                    self._queued_bytes -= taken
                residue = self._write_batch(residue, batch)
        except BaseException as exc:  # noqa: BLE001 — surfaced via writer_error
            self._writer_error = repr(exc)
            # Stop accepting so memory cannot keep growing behind a dead
            # writer; further feeds land in fed_after_close_bytes.
            with self._cv:
                self._closed = True
                self._cv.notify_all()
        finally:
            self._residue_bytes = len(residue)
            self._close_file()

    def _write_batch(self, residue: bytes, batch: list[bytes]) -> bytes:
        """Convert and append one batch. Returns the leftover partial frame.

        No retry on failure, by design. A failed ``write`` may already have put
        bytes in the file, so replaying the batch would duplicate them; and the
        realistic cause (disk full) does not improve on a second attempt.
        Aborting is affordable precisely because everything written so far is
        already safe on disk — which was not true before this module existed.
        """
        if residue:
            batch.insert(0, residue)
        if not batch:
            return b""
        data = batch[0] if len(batch) == 1 else b"".join(batch)

        usable = (len(data) // self._in_block) * self._in_block
        residue_out = data[usable:] if usable != len(data) else b""
        if usable == 0:
            return residue_out

        floats = np.frombuffer(data, dtype="<f4", count=usable // _INPUT_SAMPLE_WIDTH)

        # RMS accumulates in float64 over the ORIGINAL float32 samples. Doing
        # it on the int16 output would quantise sub-LSB signal to zero and call
        # a quiet recording silent.
        wide = floats.astype(np.float64)
        self._sumsq += float(wide @ wide)
        self._nsamples += wide.size

        out = _float32_to_int16(floats).tobytes()

        if self._written_bytes + len(out) > self._max_data_bytes:
            room = self._max_data_bytes - self._written_bytes
            room = (room // self._out_block) * self._out_block
            out = out[: max(room, 0)]
            self._size_limit_reached = True

        if out:
            fh = self._fh
            if fh is None:  # pragma: no cover - only if finish() raced badly
                raise RuntimeError("writer lost the file handle")
            # A short write here would make the patched header claim bytes the
            # file does not hold — the over-reporting the length fields exist
            # to rule out. _write_all turns it into a normal write failure, so
            # the bytes already on disk stay correct and the header is left
            # describing them.
            self._write_all(fh, out)
            fh.flush()
            self._written_bytes += len(out)
            self._written_frames += len(out) // self._out_block
            self._patch_lengths(fh)

        if self._size_limit_reached:
            raise RuntimeError(
                "WAV size limit reached (4 GiB / ~6.2h at 48kHz stereo); "
                "recording stopped at the limit "
                "/ WAV 已达 4 GiB 上限（48kHz 立体声约 6.2 小时），录制在此截止"
            )
        return residue_out

    def _close_file(self) -> None:
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        try:
            fh.flush()
        except BaseException as exc:  # noqa: BLE001 — best-effort teardown
            if self._writer_error is None:
                self._writer_error = f"close: {exc!r}"
        finally:
            with contextlib.suppress(Exception):
                fh.close()
