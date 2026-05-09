# agent/game_modes/base.py
"""
GameMode abstract base class.

Each GameMode defines: conversation flow, ending conditions, turn processing
logic, and scoring dimensions for one gameplay mode.
"""

from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from models import ConversationState, NewsContext


class GameMode(ABC):
    """Strategy for conversation flow of a single news item.

    Mode is a property of the NEWS, not user choice. Set at dispatch time
    via LiveKit job metadata.
    """

    # ── Identity (set by subclass) ────────────────────────────────────
    mode_id: str = ""            # "roast" | "debate" | "predict" | "breaking_bomb"
    display_name: str = ""       # "毒观点" | "来辩"

    # ── Abstract ──────────────────────────────────────────────────────

    @property
    @abstractmethod
    def system_prompt_extension(self) -> str:
        """Instructions appended to system prompt for this mode.

        Injected into the LLM context at turn start so the model knows
        which conversational strategy to follow.
        """

    @abstractmethod
    def get_opening(self, news: "NewsContext") -> str:
        """Generate the opening line for this game mode.

        This is what Pigugu says when the conversation begins. Must end
        with a easy-to-answer question to lower the user's speaking barrier.
        """

    @abstractmethod
    def get_max_turns(self) -> int:
        """Suggested max turns before forced ending."""

    @abstractmethod
    def should_trigger_ending(self, state: "ConversationState") -> bool:
        """Check if the ending should fire.

        Called after each user turn. Returns True when the emotional
        climax point has been reached (turn count, argument quality, etc.).
        """

    @abstractmethod
    def get_ending_line(self, state: "ConversationState") -> str:
        """The ending line Pigugu says when the climax is triggered.

        Style depends on mode: concede/claim-victory for debate,
        resonance-sign-off for roast.
        """

    @abstractmethod
    async def process_user_turn(
        self, user_message: str, state: "ConversationState"
    ) -> Optional[str]:
        """Process user input for this mode.

        Returns mode-specific context to inject into system prompt for
        the upcoming LLM call, or None if no special handling needed.

        For Debate: evaluates rebuttal quality, chooses strategy.
        For Roast: detects call for amplification vs pushback.
        """

    @abstractmethod
    def calculate_score(self, state: "ConversationState") -> dict:
        """Calculate mode-specific scoring dimensions from conversation."""
