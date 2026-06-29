"""Type definitions for roast game modes."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    POISON_OPINION = "poison_opinion"
    DEBATE = "debate"


class Phase(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"     # AI is delivering closing statement after saturated/max_turns trigger
    SETTLED = "settled"     # mark_roast_complete tool called, settlement data ready
    CLOSED = "closed"
