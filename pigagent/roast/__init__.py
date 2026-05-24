"""roast — game mode registry, state machine, and transition detection.

Usage:
    from roast import GameModeRegistry, get_game_mode, tick, consume_pending, RoastState

    GameModeRegistry.register_defaults()
    game_mode = get_game_mode("debate")
    state = RoastState.create(roast_id="r1", mode_id="debate")
"""

from __future__ import annotations

from loguru import logger

from model import Mode
from roast.base import GameMode, Trigger
from roast.state import RoastState
from roast.pending import consume as consume_pending_prompt

# ── Registry ────────────────────────────────────────────────────────────────

class GameModeRegistry:
    """Registry of all available game modes, keyed by Mode enum."""

    _modes: dict[Mode, GameMode] = {}
    _initialized: bool = False

    @classmethod
    def register(cls, mode: GameMode) -> None:
        cls._modes[mode.mode] = mode
        logger.info(f"[GameMode] Registered: {mode.mode} ({mode.display_name})")

    @classmethod
    def get(cls, mode: Mode | str) -> GameMode:
        if not cls._initialized:
            cls.register_defaults()
        if isinstance(mode, str):
            try:
                mode = Mode(mode)
            except ValueError:
                logger.warning(f"[GameMode] '{mode}' not recognized, falling back to roast_together")
                return cls._modes[Mode.ROAST_TOGETHER]
        m = cls._modes.get(mode)
        if m is None:
            logger.warning(f"[GameMode] '{mode}' not found, falling back to roast_together")
            return cls._modes[Mode.ROAST_TOGETHER]
        return m

    @classmethod
    def register_defaults(cls) -> None:
        if cls._initialized:
            return
        from roast.modes.roast_together import RoastTogetherMode
        from roast.modes.debate_bicker import DebateBickerMode
        from roast.modes.breaking_bomb import BreakingBombMode

        cls.register(RoastTogetherMode())
        cls.register(DebateBickerMode())
        cls.register(BreakingBombMode())
        cls._initialized = True


def get_game_mode(mode: Mode | str = Mode.ROAST_TOGETHER) -> GameMode:
    """Convenience: get a game mode."""
    return GameModeRegistry.get(mode)


__all__ = [
    "GameMode",
    "Trigger",
    "GameModeRegistry",
    "get_game_mode",
    "RoastState",
    "consume_pending_prompt",
]
