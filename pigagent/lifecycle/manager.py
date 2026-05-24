# pigagent/lifecycle/manager.py
"""
ConversationManager orchestrates the full lifecycle of one Pigugu conversation.

Responsibilities:
- Owns and mutates ConversationState (turn count, mood, ending, phase)
- Coordinates GameMode strategy for each user turn
- Checks and triggers ending conditions
- Provides context assembly for system prompt injection
- Manages silence/timeout detection
- Schedules post-conversation scoring and StoryCard generation
"""

import asyncio
import time
from typing import Optional, TYPE_CHECKING

from loguru import logger

from .silence_handler import SilenceHandler
from .scoring import Scorer
from .story_card import StoryCardGenerator
from .achievements import AchievementChecker
from .persistence import PersistenceProvider

if TYPE_CHECKING:
    from models import ConversationState, NewsContext
    from personas.base import Persona
    from roasts import GameMode


class ConversationManager:
    """Orchestrator for one Pigugu conversation.

    Created per agent session. Tracks all state for the current conversation
    and coordinates between Persona, GameMode, memory, scoring, persistence,
    and achievements.
    """

    def __init__(
        self,
        state: "ConversationState",
        persona: "Persona",
        game_mode: "GameMode",
        silence_handler: Optional[SilenceHandler] = None,
        scorer: Optional[Scorer] = None,
        persistence: Optional[PersistenceProvider] = None,
        achievement_checker: Optional[AchievementChecker] = None,
        device_id: str = "",
    ):
        self.state = state
        self._persona = persona
        self._game_mode = game_mode
        self._silence = silence_handler or SilenceHandler()
        self._scorer = scorer or Scorer()
        self._persistence = persistence
        self._achievements = achievement_checker or AchievementChecker()
        self._device_id = device_id
        self._scoring_scheduled: bool = False

    # ── Public hooks (called from PiguguAgent / event handlers) ──────────

    async def on_user_turn_completed(self, user_message: str) -> Optional[dict]:
        """Called after user finishes speaking, before LLM generates reply.

        Returns a dict with keys that may affect the upcoming turn:
        - ending_triggered: bool
        - ending_line: str (if ending was triggered)
        - review_tone: str (if review tone should be injected)
        - mode_context: str (optional context to inject)
        """
        user_text = user_message.strip()
        self.state.add_turn("user", user_text)

        # Reset silence tracking
        self._silence.reset()

        # Process via game mode
        mode_context = await self._game_mode.process_user_turn(
            user_text, self.state
        )

        result = {"ending_triggered": False}
        if mode_context:
            result["mode_context"] = mode_context

        # Check ending conditions
        if not self.state.ending.triggered:
            if self._game_mode.should_trigger_ending(self.state):
                await self._trigger_ending()
                result["ending_triggered"] = True
                result["ending_line"] = self._game_mode.get_ending_line(
                    self.state
                )
                result["review_tone"] = self.state.ending.render()

        return result

    def on_agent_message(self, content: str) -> None:
        """Called after the agent generates a response."""
        self.state.add_turn("assistant", content)

        # Schedule async scoring on first agent message after ending
        if self.state.ending.triggered and not self._scoring_scheduled:
            self._scoring_scheduled = True
            asyncio.create_task(self._post_conversation_scoring())

    def check_silence(self, elapsed: float) -> Optional[str]:
        """Check if a silence action should fire.

        Returns a message to speak, or None.
        """
        return self._silence.check(elapsed, self._persona)

    def reset_silence(self) -> None:
        """Reset silence state (e.g., when user speaks)."""
        self._silence.reset()

    @property
    def should_exit(self) -> bool:
        """Whether the EXIT silence stage has been reached."""
        return self._silence.should_exit

    # ── Context assembly ──────────────────────────────────────────────

    def get_mode_opening(self) -> str:
        """Get the game mode's opening line for this conversation."""
        news = self.state.news
        if news:
            return self._game_mode.get_opening(news)
        return ""

    def get_review_tone_context(self) -> str:
        """Get the review tone system prompt fragment, if active."""
        return self.state.ending.render()

    # ── Context assembly ──────────────────────────────────────────────

    async def assemble_context(self, chat_ctx, provider: str = "") -> str:
        """Build and inject the dynamic system prompt for the upcoming LLM call.

        Currently a no-op — context assembly is handled by PigAgent's
        ContextManager and the persona prompt injected by the entrypoint.
        """
        return ""

    # ── Internal ──────────────────────────────────────────────────────

    async def _trigger_ending(self) -> None:
        """Trigger the emotional ending sequence."""
        self.state.ending.trigger(self.state.turn_count)
        self.state.phase = "review"

        # Generate StoryCard asynchronously
        try:
            story_card = await StoryCardGenerator.generate(
                self.state, self._game_mode
            )
            self.state.ending.story_card = story_card.to_dict()
            logger.info(
                f"📖 [STORYCARD] Generated: {story_card.rating} | "
                f"mode={story_card.mode}"
            )
        except Exception as e:
            logger.error(f"❌ [STORYCARD] Generation failed: {e}")

        logger.info(
            f"🏁 [ENDING] Triggered at turn {self.state.turn_count}, "
            f"phase={self.state.phase}, mode={self._game_mode.mode_id}"
        )

    async def _post_conversation_scoring(self) -> None:
        """Async scoring → achievements → persistence → notify pipeline.

        Runs entirely in the background. Does not block the conversation.
        """
        try:
            # 1. Calculate scores
            score = await self._scorer.calculate(
                self.state, self._game_mode, self._persona
            )
            logger.info(
                f"📊 [SCORE] credibility={score.credibility}, "
                f"roast_points={score.roast_points}, "
                f"mode_scores={score.mode_scores}"
            )

            # Update StoryCard with credibility
            if self.state.ending.story_card:
                self.state.ending.story_card["credibility"] = score.credibility

            # 2. Check achievements
            new_achievements = self._achievements.check_all(self.state, score)
            score.achievement_ids = new_achievements
            if new_achievements:
                logger.info(f"🏆 [ACHIEVEMENT] {len(new_achievements)} unlocked: {new_achievements}")

            # 3. Persist to PostgreSQL
            if self._persistence:
                await self._persistence.save_conversation(
                    self.state, score, device_id=self._device_id
                )

            # 4. Notify FastAPI → App via Redis Pub/Sub
            if self._persistence and self._device_id:
                await self._persistence.publish_conversation_end(
                    self._device_id, self.state, score
                )
                for ach_id in new_achievements:
                    await self._persistence.publish_achievement(
                        self._device_id, ach_id
                    )

            logger.info(
                f"✅ [POST] Pipeline complete: score={score.credibility}, "
                f"achievements={len(new_achievements)}, "
                f"persist={'yes' if self._persistence and self._persistence.available else 'console'}"
            )

        except Exception as e:
            logger.error(f"❌ [POST] Pipeline failed: {e}")
