"""Tests for voice.interims.InterimBuffer.

Concurrency tests: spin up N threads each writing M items, then
drain and verify the total count. The lock guarantees no items are
lost; the deque's maxlen guarantees we don't blow up under
pathological input.
"""
import threading

import pytest

from voice.interims import InterimBuffer


def test_empty_drain():
    buf = InterimBuffer()
    assert buf.drain() == []


def test_record_and_drain():
    buf = InterimBuffer()
    buf.record("a")
    buf.record("b")
    buf.record("c")
    assert buf.drain() == ["a", "b", "c"]
    # Second drain is empty (clear-on-drain semantics)
    assert buf.drain() == []


def test_empty_strings_ignored():
    buf = InterimBuffer()
    buf.record("")
    buf.record("real")
    buf.record("")
    assert buf.drain() == ["real"]


def test_snapshot_does_not_clear():
    buf = InterimBuffer()
    buf.record("a")
    buf.record("b")
    assert buf.snapshot() == ["a", "b"]
    # Items still there
    assert buf.drain() == ["a", "b"]


def test_maxlen_caps_growth():
    buf = InterimBuffer(maxlen=3)
    for i in range(10):
        buf.record(str(i))
    # Only the most recent 3 are kept
    assert buf.snapshot() == ["7", "8", "9"]


def test_drain_as_abandoned_clears():
    buf = InterimBuffer()
    buf.record("a")
    assert buf.drain_as_abandoned() == ["a"]
    assert buf.snapshot() == []


def test_concurrent_writers_no_loss():
    """8 threads × 100 records = 800 items; the lock must
    preserve every one of them."""
    buf = InterimBuffer(maxlen=10_000)
    n_threads = 8
    n_per_thread = 100
    barrier = threading.Barrier(n_threads)

    def writer(tid: int) -> None:
        barrier.wait()  # release all at the same time
        for i in range(n_per_thread):
            buf.record(f"t{tid}:{i}")

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    items = buf.drain()
    assert len(items) == n_threads * n_per_thread
    # No duplicates, no missing
    seen = set()
    for it in items:
        assert it not in seen, f"duplicate: {it}"
        seen.add(it)
    assert len(seen) == n_threads * n_per_thread


def test_record_many():
    buf = InterimBuffer()
    buf.record_many(["a", "b", "", "c", None])  # type: ignore[arg-type]
    # None filtered (falsy), empty string filtered
    assert buf.drain() == ["a", "b", "c"]


def test_len_dunder():
    buf = InterimBuffer()
    assert len(buf) == 0
    buf.record("a")
    buf.record("b")
    assert len(buf) == 2
    buf.drain()
    assert len(buf) == 0
