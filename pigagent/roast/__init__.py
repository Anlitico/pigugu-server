"""roast  -  game mode registry, state machine, and trigger detection."""

from roast.base import GameMode, Trigger
from roast.state import RoastState
from roast.pending import consume as consume_pending_prompt
from roast.registry import GameModeRegistry, get_game_mode

__all__ = [
    "GameMode",
    "Trigger",
    "GameModeRegistry",
    "get_game_mode",
    "RoastState",
    "consume_pending_prompt",
]
