# pigagent/lifecycle/achievements.py
"""
Achievement checker — evaluates PRD-defined achievements after each conversation.

Achievements are organized by game mode (roast, debate, predict, breaking_bomb)
and cross-mode (general). Each achievement has a check function that takes
ConversationState + ScoreResult and returns bool.
"""

from typing import Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from model import ConversationState, ScoreResult


class AchievementDef:
    """Definition of one achievement."""

    def __init__(
        self,
        achievement_id: str,
        name: str,
        description: str,
        mode: str,  # "roast" | "debate" | "general"
        check_fn,
    ):
        self.id = achievement_id
        self.name = name
        self.description = description
        self.mode = mode
        self._check = check_fn

    def check(
        self, state: "ConversationState", score: "ScoreResult"
    ) -> bool:
        return self._check(state, score)


# ── Achievement definitions ─────────────────────────────────────────


def _check_first_roast(state, score):
    return state.mode_id == "roast" and state.turn_count >= 2


def _check_three_chefs_kiss(state, score):
    return score.mode_scores.get("roast_quality") == "chefs_kiss"


def _check_first_debate_win(state, score):
    return (
        state.mode_id == "debate"
        and score.mode_scores.get("debate_result") == "user_win"
    )


def _check_debate_champion(state, score):
    return (
        state.mode_id == "debate"
        and state.custom.get("strong_points", 0) >= 3
    )


def _check_first_conversation(state, score):
    return state.turn_count >= 1


def _check_engaged_user(state, score):
    return state.turn_count >= 6


def _check_night_owl(state, score):
    import datetime
    now = datetime.datetime.now()
    return now.hour < 5 or now.hour >= 0


ACHIEVEMENTS: list[AchievementDef] = [
    # Roast mode
    AchievementDef(
        "first_roast", "🔥 First Take",
        "Completed your first roast conversation",
        "roast", _check_first_roast,
    ),
    AchievementDef(
        "three_chefs_kiss", "🤌 Chef's Kiss x3",
        "Got Chef's Kiss rating in a roast",
        "roast", _check_three_chefs_kiss,
    ),
    # Debate mode
    AchievementDef(
        "first_debate_win", "🥊 First Blood",
        "Won your first debate against Pigugu",
        "debate", _check_first_debate_win,
    ),
    AchievementDef(
        "debate_champion", "👑 Debate Champion",
        "Landed 3+ strong points in a single debate",
        "debate", _check_debate_champion,
    ),
    # General
    AchievementDef(
        "first_conversation", "🎤 Breaking Silence",
        "Had your first conversation with Pigugu",
        "general", _check_first_conversation,
    ),
    AchievementDef(
        "engaged_user", "💬 Deep Dive",
        "Had a conversation with 6+ turns",
        "general", _check_engaged_user,
    ),
    AchievementDef(
        "night_owl", "🌙 Night Owl",
        "Talked to Pigugu in the wee hours",
        "general", _check_night_owl,
    ),
]


class AchievementChecker:
    """Checks all achievements against a completed conversation."""

    def __init__(self, definitions: Optional[list[AchievementDef]] = None):
        self._defs = definitions or ACHIEVEMENTS

    def check_all(
        self, state: "ConversationState", score: "ScoreResult"
    ) -> list[str]:
        """Return list of newly unlocked achievement IDs."""
        unlocked = []

        for ach in self._defs:
            # Only check achievements matching the current mode or 'general'
            if ach.mode not in ("general", state.mode_id):
                continue

            try:
                if ach.check(state, score):
                    unlocked.append(ach.id)
                    logger.info(f"🏆 [ACHIEVEMENT] Unlocked: {ach.name} ({ach.id})")
            except Exception as e:
                logger.warning(f"⚠️ [ACHIEVEMENT] Check failed for {ach.id}: {e}")

        return unlocked
