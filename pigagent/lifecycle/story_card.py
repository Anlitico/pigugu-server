# pigagent/lifecycle/story_card.py
"""
StoryCard generator — creates a narrative summary when the conversation
reaches its emotional ending point.
"""

import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from models import ConversationState


class StoryCard:
    """A narrative card generated at the conversation's ending point.

    Represents the emotional climax of the interaction — displayed in
    the app as a shareable moment.
    """

    def __init__(
        self,
        title: str,
        pigugu_quote: str,
        rating: str,
        credibility: int,
        timestamp: float,
        mode: str,
        persona: str,
    ):
        self.title = title
        self.pigugu_quote = pigugu_quote
        self.rating = rating
        self.credibility = credibility
        self.timestamp = timestamp
        self.mode = mode
        self.persona = persona

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "pigugu_quote": self.pigugu_quote,
            "rating": self.rating,
            "credibility": self.credibility,
            "timestamp": self.timestamp,
            "mode": self.mode,
            "persona": self.persona,
        }


class StoryCardGenerator:
    """Generates a StoryCard when the conversation ending triggers."""

    RATING_LABELS = {
        "chefs_kiss": "🤌 Chef's Kiss",
        "god_tier": "🔥 God-Tier Take",
        "mid": "🙄 Mid",
        "cooked": "💀 Cooked",
        "user_win": "🥊 User Won the Debate",
        "draw": "🤝 Draw",
        "pigugu_win": "👑 Pigugu Won",
    }

    @classmethod
    async def generate(
        cls, state: "ConversationState", game_mode=None
    ) -> StoryCard:
        """Generate a StoryCard from the conversation state.

        Picks the most representative agent quote and rates the interaction.
        """
        # Find the last substantial agent response as the quote
        agent_turns = [t for t in state.turns if t.role == "assistant"]
        quote = "No comment."
        if agent_turns:
            # Pick the longest or last agent response
            quote = max(agent_turns, key=lambda t: len(t.content)).content
            if len(quote) > 200:
                quote = quote[:197] + "..."

        # Determine rating
        mode_id = state.mode_id
        if game_mode:
            mode_scores = game_mode.calculate_score(state)
            if mode_id == "debate":
                rating = cls.RATING_LABELS.get(
                    mode_scores.get("debate_result", "draw"), "🙄 Mid"
                )
            else:
                rating = cls.RATING_LABELS.get(
                    mode_scores.get("roast_quality", "mid"), "🙄 Mid"
                )
        else:
            rating = "🙄 Mid"

        # Title from news context
        title = "Conversation"
        if state.news and state.news.title:
            title = state.news.title

        return StoryCard(
            title=title,
            pigugu_quote=quote,
            rating=rating,
            credibility=0,  # Filled in by Scorer
            timestamp=time.time(),
            mode=mode_id,
            persona=state.persona_id,
        )
