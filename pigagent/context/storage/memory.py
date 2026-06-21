# pigagent/context/storage/memory.py
"""L1 in-memory context store — sub-ms reads, same-session zero I/O.

Per-instance storage: each ContextManager owns its own MemoryStore.
No global dict, no TTL — lifecycle is bound to the owning ContextManager.
Async writes to L2 (Redis) and L3 (PG) are fire-and-forget — they never
block the hot path.
"""

from __future__ import annotations

from context.schema import ConversationRecord


class _UserMemory:
    """Per-instance in-memory store."""

    def __init__(self):
        self.turns: list[ConversationRecord] = []
        self.summaries: dict = {}
        self.game_state: dict = {}
        self.compressing_flag: bool = False


class MemoryStore:
    """Zero-latency context I/O. All public methods are synchronous (dict access)."""

    def __init__(self):
        self._data = _UserMemory()

    # ── Turns ───────────────────────────────────────────────────────

    def push_turn(self, record: ConversationRecord) -> None:
        self._data.turns.append(record)

    def get_hot_turns(self, n: int, *, after_anchor: int = 0) -> list[ConversationRecord]:
        # Virtual records (negative turn: L2/L3/L4 summaries) always kept
        virtual = [r for r in self._data.turns if r.turn_number <= 0]
        real = [r for r in self._data.turns if r.turn_number > 0]
        if after_anchor > 0:
            real = [r for r in real if r.turn_number > after_anchor]
        real = real[-n:] if len(real) > n else real
        return virtual + real

    def get_last_turn_number(self) -> int:
        if not self._data.turns:
            return 0
        # Only real records (>0 turn) count — virtual summaries have negative turns
        for r in reversed(self._data.turns):
            if r.turn_number > 0:
                return r.turn_number
        return 0

    def has_turns(self) -> bool:
        return bool(self._data.turns)

    # ── Compression Lock ────────────────────────────────────────────

    def is_compressing(self) -> bool:
        return self._data.compressing_flag

    def set_compressing(self, value: bool) -> None:
        self._data.compressing_flag = value

    # ── Summaries (L2 + L3 + L4) ────────────────────────────────────

    def read_summaries(self) -> dict:
        return dict(self._data.summaries)

    def write_summaries(self, end_turn: int, **kwargs) -> None:
        self._data.summaries = {"end_turn": end_turn, **kwargs}

    # ── Game State ─────────────────────────────────────────────────

    def read_game_state(self) -> dict:
        return dict(self._data.game_state)

    def write_game_state(self, state: dict) -> None:
        self._data.game_state = {**state}

    # ── Bulk load (for PG fallback recovery) ────────────────────────

    def load_all(self, records: list[ConversationRecord], summaries: dict) -> None:
        self._data.turns = list(records)
        self._data.summaries = dict(summaries)
