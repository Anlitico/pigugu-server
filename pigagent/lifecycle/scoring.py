# pigagent/lifecycle/scoring.py
"""
Post-conversation scoring pipeline.

Calculates credibility, roast points, mood delta, and game-mode-specific
scores from the completed ConversationState.
"""

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from models import ConversationState, ScoreResult
    from roasts import GameMode


class Scorer:
    """Computes post-conversation game scores.

    Scoring dimensions:
    - credibility (公信力): abstract take quality metric
    - roast_points (怼分): daily currency
    - mood_delta: how the conversation affected Pigugu's mood
    - mode_scores: game-mode-specific dimensions
    """

    # Base points per scoring tier
    CREDIBILITY_TABLE = {
        "chefs_kiss": 120,
        "god_tier": 80,
        "mid": 30,
        "cooked": 15,
    }

    ROAST_POINTS_TABLE = {
        "chefs_kiss": 3,
        "god_tier": 2,
        "mid": 1,
        "cooked": 0,
    }

    async def calculate(
        self,
        state: "ConversationState",
        game_mode: "GameMode",
        persona=None,
    ) -> "ScoreResult":
        """Calculate all scores for a completed conversation."""
        from models import ScoreResult

        credibility = self._calc_credibility(state, game_mode)
        roast_points = self._calc_roast_points(state, game_mode)
        mood_delta = self._calc_mood_delta(state)
        mode_scores = game_mode.calculate_score(state) if game_mode else {}

        return ScoreResult(
            credibility=credibility,
            roast_points=roast_points,
            mood_delta=mood_delta,
            mode_scores=mode_scores,
        )

    def _calc_credibility(
        self, state: "ConversationState", game_mode: "GameMode"
    ) -> float:
        """Calculate credibility based on user engagement and take quality."""
        user_turns = [t for t in state.turns if t.role == "user"]
        if not user_turns:
            return 0

        # Quality heuristics
        total_length = sum(len(t.content) for t in user_turns)
        avg_length = total_length / len(user_turns)

        # Longer, more substantive responses score higher
        if avg_length > 80:
            base = self.CREDIBILITY_TABLE["chefs_kiss"]
        elif avg_length > 40:
            base = self.CREDIBILITY_TABLE["god_tier"]
        elif avg_length > 15:
            base = self.CREDIBILITY_TABLE["mid"]
        else:
            base = self.CREDIBILITY_TABLE["cooked"]

        # Turn count multiplier: more engagement = better
        turn_multiplier = min(len(user_turns) / 3.0, 1.5)

        return round(base * turn_multiplier)

    def _calc_roast_points(
        self, state: "ConversationState", game_mode: "GameMode"
    ) -> int:
        """Calculate daily roast points."""
        user_turns = [t for t in state.turns if t.role == "user"]
        if not user_turns:
            return 0

        avg_length = sum(len(t.content) for t in user_turns) / len(user_turns)

        if avg_length > 80:
            return self.ROAST_POINTS_TABLE["chefs_kiss"]
        elif avg_length > 40:
            return self.ROAST_POINTS_TABLE["god_tier"]
        elif avg_length > 15:
            return self.ROAST_POINTS_TABLE["mid"]
        return self.ROAST_POINTS_TABLE["cooked"]

    def _calc_mood_delta(self, state: "ConversationState") -> dict:
        """Calculate how the conversation affected Pigugu's mood."""
        user_turns = [t for t in state.turns if t.role == "user"]
        engagement = len(user_turns)

        if engagement >= 4:
            return {"excitement": +0.1, "sarcasm": +0.05}
        elif engagement >= 2:
            return {"excitement": 0, "sarcasm": +0.02}
        return {"excitement": -0.05, "sarcasm": 0}
