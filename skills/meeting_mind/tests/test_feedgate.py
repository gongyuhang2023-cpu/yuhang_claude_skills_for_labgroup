"""Test F — the barrier must be correct under load and must never hang.

FeedGate sits directly in the audio callback path, so a bug here does not
produce a wrong file, it wedges the recording. Every test in this module is
written so that a deadlock shows up as a failed assertion within seconds
rather than a hung test run.
"""

from __future__ import annotations

import threading
import time

from meetingmind.wavsink import FeedGate


def test_enter_leave_round_trip() -> None:
    gate = FeedGate()
    assert gate.enter(100) is True
    gate.leave()
    assert gate.close(timeout=1.0) is True
    assert gate.late_calls == 0


def test_enter_after_close_is_rejected_and_counted() -> None:
    gate = FeedGate()
    assert gate.close(timeout=1.0) is True

    for i in range(5):
        assert gate.enter(10 * (i + 1)) is False

    assert gate.accepting is False
    assert gate.late_calls == 5
    assert gate.late_bytes == 10 + 20 + 30 + 40 + 50


def test_close_is_idempotent() -> None:
    gate = FeedGate()
    assert gate.close(timeout=1.0) is True
    assert gate.close(timeout=1.0) is True
    assert gate.late_calls == 0


def test_close_waits_for_in_flight_caller() -> None:
    gate = FeedGate()
    released = threading.Event()
    entered = threading.Event()

    def worker() -> None:
        assert gate.enter(1) is True
        entered.set()
        released.wait(5)
        gate.leave()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert entered.wait(5)

    # Nobody has left yet, so a short close must report failure rather than
    # silently pretending the producer is idle.
    assert gate.close(timeout=0.2) is False

    released.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert gate.close(timeout=2.0) is True


def test_close_returns_false_instead_of_hanging() -> None:
    """A caller that never leaves must cost a timeout, not the process."""
    gate = FeedGate()
    assert gate.enter(1) is True  # deliberately never left

    t0 = time.monotonic()
    ok = gate.close(timeout=0.3)
    elapsed = time.monotonic() - t0

    assert ok is False
    assert 0.25 < elapsed < 3.0, f"close() took {elapsed:.2f}s"


def test_many_threads_then_close_is_race_free() -> None:
    """Twenty threads hammering the gate, then one close().

    Workers exit *only* by being rejected — there is deliberately no other stop
    signal. That makes the counts exact instead of depending on which thread
    the scheduler happened to run: if the gate ever fails to reject after
    close(), the workers spin, the joins time out, and the test says so rather
    than hanging.
    """
    gate = FeedGate()
    n_threads = 20
    passed = [0] * n_threads
    rejected = [0] * n_threads
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            while True:
                if gate.enter(8):
                    passed[idx] += 1
                    gate.leave()
                else:
                    rejected[idx] += 1
                    return  # gate closed for good; nothing more to do
        except BaseException as exc:  # pragma: no cover - would be a real bug
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True)
        for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    time.sleep(0.2)  # let them hammer the gate

    assert gate.close(timeout=5.0) is True
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "a worker is stuck — the gate deadlocked"

    assert not errors
    assert sum(passed) > 0, "the workers never actually got through"
    # Each thread runs until it is turned away, exactly once.
    assert sum(rejected) == n_threads
    assert gate.late_calls == n_threads
    assert gate.late_bytes == n_threads * 8


def test_no_entries_are_admitted_after_close_returns() -> None:
    """The barrier's actual guarantee: once close() returns, nobody gets in.

    Note what is deliberately NOT asserted: a caller already inside may well
    observe ``accepting == False`` before it leaves. That is close() doing its
    job — setting the flag and then waiting for the straggler — not a leak.
    """
    gate = FeedGate()
    closed_returned = threading.Event()
    violations: list[int] = []
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            if gate.enter(1):
                if closed_returned.is_set():
                    violations.append(1)  # admitted after the barrier stood
                gate.leave()
            else:
                return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.1)

    assert gate.close(timeout=5.0) is True
    closed_returned.set()
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert violations == []
    # And a second close is instant, proving the active count really hit zero.
    t0 = time.monotonic()
    assert gate.close(timeout=5.0) is True
    assert time.monotonic() - t0 < 0.1


def test_unmatched_leave_does_not_wedge_close() -> None:
    """Defensive: a stray leave() must not drive the counter negative.

    If it did, the active count would never reach zero again and close() would
    burn its whole timeout on every subsequent stop.
    """
    gate = FeedGate()
    gate.leave()
    gate.leave()
    assert gate.enter(1) is True
    gate.leave()
    assert gate.close(timeout=1.0) is True
