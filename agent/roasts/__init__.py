# agent/roasts/__init__.py
"""Game mode registry for Pigugu gameplay modes."""

from loguru import logger

from .base import GameMode
from .roast import RoastMode
from .debate import DebateMode
from .predict import PredictMode
from .breaking_bomb import BreakingBombMode


class GameModeRegistry:
    """Registry of all available game modes, keyed by mode_id."""

    _modes: dict[str, GameMode] = {}
    _initialized: bool = False

    @classmethod
    def register(cls, mode: GameMode) -> None:
        """Register a game mode."""
        cls._modes[mode.mode_id] = mode
        logger.info(f"✅ Registered game mode: {mode.mode_id} ({mode.display_name})")

    @classmethod
    def get(cls, mode_id: str) -> GameMode:
        """Get a game mode by ID. Falls back to 'roast' if not found."""
        if not cls._initialized:
            cls.register_defaults()
        mode = cls._modes.get(mode_id)
        if mode is None:
            logger.warning(
                f"Game mode '{mode_id}' not found, falling back to 'roast'"
            )
            return cls._modes["roast"]
        return mode

    @classmethod
    def list_ids(cls) -> list[str]:
        """Return all registered mode IDs."""
        if not cls._initialized:
            cls.register_defaults()
        return list(cls._modes.keys())

    @classmethod
    def register_defaults(cls) -> None:
        """Register all built-in game modes."""
        if cls._initialized:
            return
        cls.register(RoastMode())
        cls.register(DebateMode())
        cls.register(PredictMode())
        cls.register(BreakingBombMode())
        cls._initialized = True


def get_game_mode(mode_id: str = "roast") -> GameMode:
    """Convenience function: get a game mode by ID."""
    return GameModeRegistry.get(mode_id)


__all__ = [
    "GameMode",
    "GameModeRegistry",
    "RoastMode",
    "DebateMode",
    "PredictMode",
    "BreakingBombMode",
    "get_game_mode",
]
