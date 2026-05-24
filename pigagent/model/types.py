"""Shared type definitions for pigagent."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    ROAST_TOGETHER = "roast_together"
    DEBATE_BICKER = "debate_bicker"
    BREAKING_BOMB = "breaking_bomb"


class Phase(StrEnum):
    ACTIVE = "active"
    REVIEW = "review"
    CLOSED = "closed"
