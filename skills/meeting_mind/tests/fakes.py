"""Test doubles for the pieces that touch the outside world.

File-level fakes live here from the start because WavSink's error contract
("a failed write aborts the recording and leaves a valid, truncated file")
cannot be tested any other way — we will not fill a real disk to see it.

Device-level fakes (ProcTap / soundcard / sounddevice) are added alongside the
adapter migrations that need them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    import numpy as np


class _WrappedFile:
    """Base for file wrappers: forward everything we do not override."""

    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh
        self.write_calls = 0
        self.flush_calls = 0

    # -- the subset WavSink actually uses -------------------------------
    def write(self, data: bytes) -> int:
        self.write_calls += 1
        return self._fh.write(data)

    def flush(self) -> None:
        self.flush_calls += 1
        self._fh.flush()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fh.seek(offset, whence)

    def tell(self) -> int:
        return self._fh.tell()

    def fileno(self) -> int:
        return self._fh.fileno()

    def close(self) -> None:
        self._fh.close()

    @property
    def closed(self) -> bool:
        return self._fh.closed


class FaultyFile(_WrappedFile):
    """Raises on the Nth write() (1-based), like a disk filling up mid-recording.

    ENOSPC is the realistic case: it is not transient, so retrying would be
    pointless — which is exactly why WavSink aborts instead of retrying.
    """

    def __init__(
        self,
        fh: BinaryIO,
        fail_on_write: int,
        exc: BaseException | None = None,
    ) -> None:
        super().__init__(fh)
        self._fail_on_write = fail_on_write
        self._exc = exc or OSError(28, "No space left on device")

    def write(self, data: bytes) -> int:
        self.write_calls += 1
        if self.write_calls >= self._fail_on_write:
            raise self._exc
        return self._fh.write(data)


class FaultyFlushFile(_WrappedFile):
    """Raises on the Nth flush() — writes succeed but never reach the disk."""

    def __init__(
        self,
        fh: BinaryIO,
        fail_on_flush: int,
        exc: BaseException | None = None,
    ) -> None:
        super().__init__(fh)
        self._fail_on_flush = fail_on_flush
        self._exc = exc or OSError(28, "No space left on device")

    def flush(self) -> None:
        self.flush_calls += 1
        if self.flush_calls >= self._fail_on_flush:
            raise self._exc
        self._fh.flush()


class ShortWriteFile(_WrappedFile):
    """Stores only part of what it is given, and does not complain.

    The nasty case: no exception, just a smaller return value. A writer that
    ignores the return value would count the full length and patch the header
    to claim bytes that never landed — over-reporting, which is the one thing
    the length fields must never do.
    """

    def __init__(self, fh: BinaryIO, short_on_write: int, fraction: float = 0.5) -> None:
        super().__init__(fh)
        self._short_on_write = short_on_write
        self._fraction = fraction

    def write(self, data: bytes) -> int:
        self.write_calls += 1
        if self.write_calls >= self._short_on_write and len(data) > 1:
            partial = max(1, int(len(data) * self._fraction))
            self._fh.write(data[:partial])
            return partial
        return self._fh.write(data)


class BlockingWriteFile(_WrappedFile):
    """Blocks forever inside write() once armed, wedging the writer thread."""

    def __init__(self, fh: BinaryIO, block_on_write: int) -> None:
        super().__init__(fh)
        self._block_on_write = block_on_write
        self.release = threading.Event()
        self.blocking = threading.Event()

    def write(self, data: bytes) -> int:
        self.write_calls += 1
        if self.write_calls >= self._block_on_write:
            self.blocking.set()
            self.release.wait(60)
        return self._fh.write(data)


class SlowFile(_WrappedFile):
    """Sleeps on every write, simulating a stalled disk.

    Used to drive the producer-outruns-writer case: with a bounded queue the
    sink must shed data and stay memory-bounded rather than buffering without
    limit — the failure mode this whole refactor exists to remove.
    """

    def __init__(self, fh: BinaryIO, delay: float = 0.005) -> None:
        super().__init__(fh)
        self._delay = delay

    def write(self, data: bytes) -> int:
        self.write_calls += 1
        time.sleep(self._delay)
        return self._fh.write(data)


class NullFile:
    """Tracks position and size but discards the bytes.

    For the volume tests: proving the sink is memory-bounded needs two hours of
    audio pushed through the queue and the conversion, but does not need 1.4 GB
    actually landing on a disk every test run. The byte-exactness of what would
    have been written is covered separately against a real file.
    """

    def __init__(self) -> None:
        self._pos = 0
        self._size = 0
        self.write_calls = 0
        self.flush_calls = 0
        self.bytes_written = 0
        self.closed = False

    def write(self, data: bytes) -> int:
        self.write_calls += 1
        n = len(data)
        self._pos += n
        self.bytes_written += n
        self._size = max(self._size, self._pos)
        return n

    def flush(self) -> None:
        self.flush_calls += 1

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        else:
            self._pos = self._size + offset
        return self._pos

    def tell(self) -> int:
        return self._pos

    def close(self) -> None:
        self.closed = True


class FakeLoopbackStream:
    """Stands in for soundcard's blocking `recorder.record(numframes)`.

    Hands out a scripted signal one block at a time, then goes quiet the way a
    real endpoint does when nothing is playing. `stall_seconds` makes it hang
    instead, to exercise the path where the capture thread cannot be joined.
    """

    def __init__(
        self,
        data: np.ndarray | None = None,
        stall_seconds: float = 0.0,
        quiet_sleep: float = 0.005,
    ) -> None:
        self._data = data
        self._pos = 0
        self._stall_seconds = stall_seconds
        self._quiet_sleep = quiet_sleep
        self.blocks_served = 0
        self.exhausted = threading.Event()
        # Set once the capture thread is genuinely parked inside a stalled
        # record(). Without it a test that flips `stall_seconds` and calls
        # stop() immediately usually wins the race, the thread never reaches
        # the stall, and the join it meant to time out succeeds instead.
        self.stalling = threading.Event()
        if data is None:
            self.exhausted.set()

    def record(self, numframes: int) -> np.ndarray | None:
        if self._stall_seconds:
            self.stalling.set()
            time.sleep(self._stall_seconds)
            return None
        if self._data is None or self._pos >= len(self._data):
            self.exhausted.set()
            time.sleep(self._quiet_sleep)
            return None
        block = self._data[self._pos : self._pos + numframes]
        self._pos += numframes
        self.blocks_served += 1
        return block


class FakeMicrophone:
    def __init__(
        self,
        name: str = "Fake Loopback (Speakers)",
        data: np.ndarray | None = None,
        stall_seconds: float = 0.0,
        open_error: BaseException | None = None,
    ) -> None:
        self.name = name
        self.isloopback = True
        self._data = data
        self._stall_seconds = stall_seconds
        self._open_error = open_error
        self.stream: FakeLoopbackStream | None = None

    def recorder(self, samplerate: int, channels: int, blocksize: int):  # noqa: ANN201
        if self._open_error is not None:
            raise self._open_error
        self.stream = FakeLoopbackStream(self._data, self._stall_seconds)
        stream = self.stream

        class _Ctx:
            def __enter__(self):  # noqa: ANN204
                return stream

            def __exit__(self, *_exc: object) -> None:
                return None

        return _Ctx()


class FakeProcTap:
    """Stands in for `proctap.ProcessAudioCapture`, warts included.

    The wart that matters: `late_seconds` makes `stop()` return while the
    worker keeps delivering. That is not a strawman — the real
    `ProcessAudioCapture.stop()` joins its worker with a one-second timeout and
    then drops the thread reference regardless, so a backend read that takes
    longer leaves a live thread nobody can wait for.
    """

    def __init__(
        self,
        pid: int,
        on_data: Callable[[bytes, int], None],
        data: bytes | None = None,
        blocksize: int = 4800 * 2 * 4,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
        late_seconds: float = 0.0,
    ) -> None:
        self.pid = pid
        self._on_data = on_data
        self._data = data
        self._blocksize = blocksize
        self._start_error = start_error
        self._stop_error = stop_error
        self._late_seconds = late_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.exhausted = threading.Event()
        self.worker_finished = threading.Event()
        self.late_deliveries = 0
        self.closed = False

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        pos = 0
        data = self._data or b""
        while not self._stop.is_set() and pos < len(data):
            block = data[pos : pos + self._blocksize]
            self._on_data(block, len(block) // 8)
            pos += self._blocksize
        self.exhausted.set()

        if self._late_seconds:
            # stop() has been told to lie; keep shipping audio at it.
            deadline = time.monotonic() + self._late_seconds
            while time.monotonic() < deadline:
                self._on_data(b"\x00" * 1024, 128)
                self.late_deliveries += 1
                time.sleep(0.01)
        else:
            while not self._stop.is_set():
                time.sleep(0.005)
        self.worker_finished.set()

    def stop(self) -> None:
        if self._stop_error is not None:
            raise self._stop_error
        if self._late_seconds:
            # Mirrors the real stop(): give up on the worker, return anyway.
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def close(self) -> None:
        self.closed = True
        if not self._late_seconds:
            self.stop()


def proctap_factory(**kwargs):  # noqa: ANN003, ANN201
    """Build a FakeProcTap factory that records the instance it creates."""
    created: list[FakeProcTap] = []

    def _make(pid: int, on_data: Callable[[bytes, int], None]) -> FakeProcTap:
        tap = FakeProcTap(pid=pid, on_data=on_data, **kwargs)
        created.append(tap)
        return tap

    _make.created = created  # type: ignore[attr-defined]
    return _make


class FakeInputStream:
    """Stands in for `sounddevice.InputStream`, callback thread and all.

    Crucially it reuses one buffer and scribbles over it as soon as the
    callback returns, exactly as PortAudio does. Anything that keeps a view of
    `indata` instead of copying will therefore produce garbage — which is the
    only way to catch that mistake short of a real device.
    """

    def __init__(
        self,
        samplerate: int,
        channels: int,
        device: object,
        dtype: str,
        callback: Callable[..., None],
        data: np.ndarray | None = None,
        blocksize: int = 1024,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.device = device
        self.dtype = dtype
        self._callback = callback
        self._data = data
        self._blocksize = blocksize
        self._start_error = start_error
        self._stop_error = stop_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.exhausted = threading.Event()
        self.closed = False
        # Counts how many times the shared buffer was clobbered after a
        # callback. Tests assert on it so "the copy is happening" cannot pass
        # merely because the hazard was never simulated.
        self.overwrites = 0

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        import numpy as np  # local: fakes must not force numpy on importers

        if self._data is None:
            self.exhausted.set()
        else:
            buf = np.zeros((self._blocksize, self.channels), dtype=np.float32)
            pos = 0
            while not self._stop.is_set() and pos < len(self._data):
                block = self._data[pos : pos + self._blocksize]
                n = len(block)
                buf[:n, 0] = block
                self._callback(buf[:n], n, None, None)
                # PortAudio owns this memory and will reuse it immediately.
                buf.fill(float("nan"))
                self.overwrites += 1
                pos += n
            self.exhausted.set()
        while not self._stop.is_set():
            time.sleep(0.005)

    def stop(self) -> None:
        if self._stop_error is not None:
            raise self._stop_error
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def close(self) -> None:
        self._stop.set()
        self.closed = True


class FakeSoundDeviceModule:
    """Minimal stand-in for the `sounddevice` module, injected via sys.modules."""

    def __init__(
        self,
        data: np.ndarray | None = None,
        default_samplerate: float | None = 44100.0,
        query_error: BaseException | None = None,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
        blocksize: int = 1024,
    ) -> None:
        self._data = data
        self._default_samplerate = default_samplerate
        self._query_error = query_error
        self._start_error = start_error
        self._stop_error = stop_error
        self._blocksize = blocksize
        self.stream: FakeInputStream | None = None

    def query_devices(self, device: object = None, kind: str | None = None):  # noqa: ANN201, ARG002
        if self._query_error is not None:
            raise self._query_error
        return {"default_samplerate": self._default_samplerate}

    def InputStream(  # noqa: N802 — mirrors the sounddevice API
        self,
        samplerate: int,
        channels: int,
        device: object,
        dtype: str,
        callback: Callable[..., None],
    ) -> FakeInputStream:
        self.stream = FakeInputStream(
            samplerate,
            channels,
            device,
            dtype,
            callback,
            data=self._data,
            blocksize=self._blocksize,
            start_error=self._start_error,
            stop_error=self._stop_error,
        )
        return self.stream


class FakeSoundcardModule:
    """Minimal stand-in for the `soundcard` module, injected via sys.modules."""

    def __init__(self, mic: FakeMicrophone | None = None) -> None:
        self.mic = mic or FakeMicrophone()

    def default_speaker(self):  # noqa: ANN201
        class _Speaker:
            name = "Fake Speakers"

        return _Speaker()

    def get_microphone(self, name: str, include_loopback: bool = False):  # noqa: ANN201, ARG002
        return self.mic

    def all_microphones(self, include_loopback: bool = False):  # noqa: ANN201, ARG002
        return [self.mic]


def null_opener() -> Callable[[Path], BinaryIO]:
    def _open(_path: Path) -> BinaryIO:
        return NullFile()  # type: ignore[return-value]

    return _open


def faulty_opener(
    fail_on_write: int, exc: BaseException | None = None
) -> Callable[[Path], BinaryIO]:
    def _open(path: Path) -> BinaryIO:
        return FaultyFile(open(path, "wb"), fail_on_write, exc)  # type: ignore[return-value]

    return _open


def faulty_flush_opener(
    fail_on_flush: int, exc: BaseException | None = None
) -> Callable[[Path], BinaryIO]:
    def _open(path: Path) -> BinaryIO:
        return FaultyFlushFile(open(path, "wb"), fail_on_flush, exc)  # type: ignore[return-value]

    return _open


def slow_opener(delay: float = 0.005) -> Callable[[Path], BinaryIO]:
    def _open(path: Path) -> BinaryIO:
        return SlowFile(open(path, "wb"), delay)  # type: ignore[return-value]

    return _open


def short_write_opener(
    short_on_write: int, fraction: float = 0.5
) -> Callable[[Path], BinaryIO]:
    def _open(path: Path) -> BinaryIO:
        return ShortWriteFile(open(path, "wb"), short_on_write, fraction)  # type: ignore[return-value]

    return _open


def blocking_write_opener(block_on_write: int) -> tuple[Callable[[Path], BinaryIO], list]:
    """Returns (opener, created) so a test can release the block later."""
    created: list[BlockingWriteFile] = []

    def _open(path: Path) -> BinaryIO:
        # No context manager on purpose: an opener hands the handle's lifetime
        # to WavSink, which closes it in its writer thread.
        fh = BlockingWriteFile(open(path, "wb"), block_on_write)  # noqa: SIM115
        created.append(fh)
        return fh  # type: ignore[return-value]

    return _open, created
