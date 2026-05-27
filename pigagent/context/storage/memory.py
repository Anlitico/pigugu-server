# pigagent/context/storage/memory.py
"""L1 in-memory context store — sub-ms reads, same-session zero I/O.

TTL-evicted after 30 min of inactivity. Async writes to L2 (Redis) and L3 (PG)
are fire-and-forget — they never block the hot path.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from loguru import logger

from config import get_config

_cfg = get_config()

from context.schema import ConversationRecord


_MEMORY: dict[str, "_UserMemory"] = {}
_LOCK = threading.Lock()
_TTL_SECONDS = 1800  # 30 min
_CLEANUP_INTERVAL = 300  # 5 min
_cleanup_task: asyncio.Task | None = None


class _UserMemory:
    """Per-user in-memory store."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.turns: list[ConversationRecord] = []
        self.summaries: dict = {}
        self.game_state: dict = {}
        self.compressing_flag: bool = False
        self.last_access = time.monotonic()

    def touch(self):
        self.last_access = time.monotonic()

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.last_access > _TTL_SECONDS


def _ensure_cleanup():
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        try:
            loop = asyncio.get_running_loop()
            _cleanup_task = loop.create_task(_auto_cleanup())
        except RuntimeError:
            pass  # no running loop


async def _auto_cleanup():
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        with _LOCK:
            expired = [uid for uid, um in _MEMORY.items() if um.expired]
            for uid in expired:
                del _MEMORY[uid]
            if expired:
                logger.debug(f"[MemoryStore] Evicted {len(expired)} inactive users")


class MemoryStore:
    """Zero-latency context I/O. All public methods are synchronous (dict access)."""

    def __init__(self, user_id: str):
        self._user_id = user_id
        _ensure_cleanup()

    # ── Turns ───────────────────────────────────────────────────────

    def push_turn(self, record: ConversationRecord) -> None:
        um = self._get_or_create()
        um.turns.append(record)
        window = _cfg.CONTEXT_HOT_WINDOW_SIZE
        if len(um.turns) > window:
            um.turns = um.turns[-window:]

    def get_hot_turns(self, n: int, *, after_anchor: int = 0) -> list[ConversationRecord]:
        um = self._get()
        if not um:
            return []
        # Virtual records (negative turn: L2/L3/L4 summaries) always kept
        virtual = [r for r in um.turns if r.turn_number <= 0]
        real = [r for r in um.turns if r.turn_number > 0]
        if after_anchor > 0:
            real = [r for r in real if r.turn_number > after_anchor]
        real = real[-n:] if len(real) > n else real
        return virtual + real

    def get_last_turn_number(self) -> int:
        um = self._get()
        if not um or not um.turns:
            return 0
        # Only real records (>0 turn) count — virtual summaries have negative turns
        for r in reversed(um.turns):
            if r.turn_number > 0:
                return r.turn_number
        return 0

    def has_turns(self) -> bool:
        um = self._get()
        return bool(um and um.turns)

    # ── Compression Lock ────────────────────────────────────────────

    def is_compressing(self) -> bool:
        um = self._get()
        return um.compressing_flag if um else False

    def set_compressing(self, value: bool) -> None:
        um = self._get_or_create()
        um.compressing_flag = value

    # ── Summaries (L2 + L3 + L4) ────────────────────────────────────

    def read_summaries(self) -> dict:
        um = self._get()
        return dict(um.summaries) if um else {}

    def write_summaries(self, end_turn: int, **kwargs) -> None:
        um = self._get_or_create()
        um.summaries = {"end_turn": end_turn, **kwargs}

    # ── Game State ─────────────────────────────────────────────────

    def read_game_state(self) -> dict:
        um = self._get()
        return dict(um.game_state) if um else {}

    def write_game_state(self, state: dict) -> None:
        um = self._get_or_create()
        um.game_state = {**state}

    # ── Bulk load (for PG fallback recovery) ────────────────────────

    def load_all(self, records: list[ConversationRecord], summaries: dict) -> None:
        um = self._get_or_create()
        um.turns = list(records)
        um.summaries = dict(summaries)

    # ── Internal ───────────────────────────────────────────────────

    def _get(self) -> _UserMemory | None:
        um = _MEMORY.get(self._user_id)
        if um:
            um.touch()
        return um

    def _get_or_create(self) -> _UserMemory:
        with _LOCK:
            um = _MEMORY.get(self._user_id)
            if um is None:
                um = _UserMemory(self._user_id)
                _MEMORY[self._user_id] = um
            um.touch()
            return um


def drop_user(user_id: str) -> None:
    with _LOCK:
        _MEMORY.pop(user_id, None)


def clear_all() -> None:
    with _LOCK:
        _MEMORY.clear()
        logger.debug("[MemoryStore] Cleared all memory")
