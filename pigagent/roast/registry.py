"""GameModeRegistry  -  load and register all game modes at startup."""

from __future__ import annotations

from loguru import logger
from roast.types import Mode
from roast.base import GameMode


class GameModeRegistry:
    """Registry of all available game modes, keyed by Mode enum.

    Call register_defaults() once at startup, then get() to resolve
    a mode string (from metadata) to a GameMode instance.
    """

    _modes: dict[Mode, GameMode] = {}
    _initialized: bool = False

    @classmethod
    def register(cls, mode: GameMode) -> None:
        cls._modes[mode.mode] = mode
        logger.info(f"[GameMode] Registered: {mode.mode}")

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

        cls.register(RoastTogetherMode())
        cls.register(DebateBickerMode())
        cls._initialized = True


    @classmethod
    def build_cache(cls) -> dict[str, GameMode]:
        """Pre-build {mode_id: GameMode} for all registered modes."""
        if not cls._initialized:
            cls.register_defaults()
        return {str(k): v for k, v in cls._modes.items()}


def get_game_mode(mode: Mode | str = Mode.ROAST_TOGETHER) -> GameMode:
    """Convenience: get a game mode."""
    return GameModeRegistry.get(mode)
