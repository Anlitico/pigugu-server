"""Thread-safe interim STT transcript buffer.

Deepgram's ``on_message`` callback runs in a background thread. The barge-in
path is already fire-and-forget via ``asyncio.run_coroutine_threadsafe``;
the new "store every interim for the sidecar JSON" path needs the same
treatment, but it can be even simpler: the interim is just text — we
append it under a lock, and the asyncio side drains it at turn commit.

Why a deque + Lock and not an asyncio.Queue:
- ``asyncio.Queue`` is loop-bound; the producer thread can't put into it
  without ``loop.call_soon_threadsafe`` (extra hop, scheduler overhead).
- We never read from this buffer inside the Deepgram thread, only from
  the asyncio loop. So a ``collections.deque`` + ``threading.Lock`` is
  the minimum-overhead primitive.

Why an explicit class (not just a deque):
- A typed wrapper documents the lifecycle (drain on final, drain on
  barge-in) and centralizes the threading rules. The tests in
  ``tests/voice/test_interims.py`` exercise concurrent appenders, which
  would be awkward on a raw deque.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Iterable


class InterimBuffer:
    """Append-only thread-safe text buffer with explicit drain semantics.

    Lifecycle in the voice pipeline:
    1. ``record(text)`` from the Deepgram thread on every interim message.
    2. ``drain()`` from the asyncio loop on STT final — moves the entire
       buffer into ``TurnStorage.stt_interims``.
    3. ``drain_as_abandoned()`` from the asyncio loop on barge-in —
       moves the entire buffer into ``TurnStorage.abandoned_stts`` and
       resets state for the next turn.

    The buffer is bounded (``maxlen``) to protect against runaway growth
    in pathological cases (e.g. STT never finalizes for hours). When the
    buffer is full, the oldest entry is dropped — the newest transcripts
    are more diagnostic anyway.
    """

    __slots__ = ("_deque", "_lock", "_maxlen")

    def __init__(self, maxlen: int = 1024) -> None:
        self._deque: deque[str] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._maxlen = maxlen

    def record(self, text: str) -> None:
        """Append one interim text. Safe to call from any thread."""
        if not text:
            return
        with self._lock:
            self._deque.append(text)

    def record_many(self, texts: Iterable[str]) -> None:
        """Append a sequence of interim texts. Safe to call from any thread."""
        with self._lock:
            for t in texts:
                if t:
                    self._deque.append(t)

    def drain(self) -> list[str]:
        """Atomically read and clear the buffer. Returns the interims
        captured since the last drain (or since construction)."""
        with self._lock:
            out = list(self._deque)
            self._deque.clear()
            return out

    def drain_as_abandoned(self) -> list[str]:
        """Same as ``drain()`` but semantically marks these as orphaned
        (barge-in / never-became-final). The caller decides where to
        store the result; this method only enforces the lock + clear."""
        return self.drain()

    def snapshot(self) -> list[str]:
        """Read without clearing. Mostly for diagnostics."""
        with self._lock:
            return list(self._deque)

    def __len__(self) -> int:
        with self._lock:
            return len(self._deque)

    def __repr__(self) -> str:  # pragma: no cover
        with self._lock:
            return f"InterimBuffer(len={len(self._deque)}, maxlen={self._maxlen})"
