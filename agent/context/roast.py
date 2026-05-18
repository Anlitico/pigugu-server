# agent/context/roast.py
"""Roast state — pure functions over ConversationRecord lists.

Roast ends when:
  1. A new roast_id appears (starts a different roast)
  2. The roast is stale: current turn is too far from the roast prompt (default 24h)
"""

from __future__ import annotations

from context.schema import ConversationRecord


class RoastState:
    """Pure functions. Input = list[ConversationRecord], oldest→newest."""

    _STALE_SECONDS = 24 * 3600  # 24h

    # ── Assignment ────────────────────────────────────────────────────

    @staticmethod
    def assign_roast_id(history: list[ConversationRecord], current: ConversationRecord) -> str | None:
        """Assign roast_id to the current record.

        1. current already has roast_id → keep it (roast prompt)
        2. Previous roast is active (not stale) → inherit
        3. Otherwise → None (free chat)
        """
        if current.roast_id:
            return current.roast_id

        prev = RoastState.current_roast_id(history)

        if prev and not RoastState._is_stale(history, current):
            current.roast_id = prev
            return prev

        current.roast_id = None
        return None

    # ── State queries ────────────────────────────────────────────────

    @staticmethod
    def current_roast_id(records: list[ConversationRecord]) -> str | None:
        if records:
            return records[-1].roast_id
        return None

    @staticmethod
    def is_active(records: list[ConversationRecord]) -> bool:
        return bool(records) and records[-1].roast_id is not None

    # ── Internal ──────────────────────────────────────────────────────

    @staticmethod
    def _is_stale(history: list[ConversationRecord], current: ConversationRecord | None) -> bool:
        """True if current time is > STALE_SECONDS after the roast started."""
        rid = RoastState.current_roast_id(history)
        if not rid:
            return True
        for r in history:
            if r.roast_id == rid:
                ts = current.created_at if current else history[-1].created_at
                return (ts - r.created_at) > RoastState._STALE_SECONDS
        return True
