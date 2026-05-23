# pigagent/lifecycle/persistence.py
"""
Persistence layer — PostgreSQL write + Redis Pub/Sub notification.

Connects the agent's post-conversation scoring pipeline to the
FastAPI backend via Redis Pub/Sub and durable PostgreSQL storage.
"""

import json
from typing import Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from models import ConversationState, ScoreResult


class PersistenceProvider:
    """Handles durable storage and cross-process notification.

    PostgreSQL tables (expected schema, created by FastAPI migrations):
        conversation_records  (id, user_id, device_id, persona, mode,
                               turns_json, story_card_json, created_at)
        user_game_state       (user_id, credibility, roast_points,
                               mood_label, updated_at)
        achievements          (user_id, achievement_id, unlocked_at)

    Redis channels:
        pigugu:events:{device_id}  → FastAPI WebSocket → App push
    """

    def __init__(
        self,
        pg_pool=None,
        redis_client=None,
    ):
        self._pg = pg_pool
        self._redis = redis_client
        self._available = bool(pg_pool or redis_client)

    @property
    def available(self) -> bool:
        return self._available

    # ── PostgreSQL writes ───────────────────────────────────────────

    async def save_conversation(
        self,
        state: "ConversationState",
        score: "ScoreResult",
        device_id: str = "",
    ) -> Optional[str]:
        """Persist conversation record and update user game state.

        Returns the new conversation record ID, or None if persistence
        is unavailable.
        """
        if not self._pg:
            logger.info("📝 [PERSIST] PG not available — conversation logged to console only")
            self._log_conversation(state, score)
            return None

        try:
            # conversation_records insert
            turns_json = json.dumps([
                {"role": t.role, "content": t.content, "turn": t.turn_number}
                for t in state.turns
            ], ensure_ascii=False)

            story_card_json = json.dumps(
                state.ending.story_card or {}, ensure_ascii=False
            )

            async with self._pg.acquire() as conn:
                record_id = await conn.fetchval(
                    """
                    INSERT INTO conversation_records
                        (user_id, device_id, persona_id, mode_id,
                         turns_json, story_card_json, credibility, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                    RETURNING id
                    """,
                    state.user_id or "unknown",
                    device_id,
                    state.persona_id,
                    state.mode_id,
                    turns_json,
                    story_card_json,
                    int(score.credibility),
                )

                # Upsert user_game_state
                await conn.execute(
                    """
                    INSERT INTO user_game_state (user_id, credibility, roast_points,
                        mood_label, updated_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        credibility = user_game_state.credibility + $2,
                        roast_points = user_game_state.roast_points + $3,
                        mood_label = $4,
                        updated_at = NOW()
                    """,
                    state.user_id or "unknown",
                    int(score.credibility),
                    score.roast_points,
                    state.mood.label if state.mood else "default",
                )

            logger.info(
                f"💾 [PERSIST] Saved conversation {record_id}: "
                f"credibility={score.credibility}, roast={score.roast_points}"
            )
            return str(record_id)

        except Exception as e:
            logger.error(f"❌ [PERSIST] PostgreSQL write failed: {e}")
            self._log_conversation(state, score)
            return None

    def _log_conversation(
        self, state: "ConversationState", score: "ScoreResult"
    ) -> None:
        """Fallback: log full conversation to console."""
        logger.info("=" * 60)
        logger.info("CONVERSATION RECORD (no persistence)")
        logger.info(f"  Session: {state.session_id}")
        logger.info(f"  Persona: {state.persona_id} | Mode: {state.mode_id}")
        logger.info(f"  Turns: {state.turn_count}")
        logger.info(f"  Credibility: {score.credibility}")
        logger.info(f"  Roast Points: {score.roast_points}")
        logger.info(f"  Mode Scores: {score.mode_scores}")
        if state.ending.story_card:
            logger.info(f"  StoryCard: {state.ending.story_card.get('rating')}")
        logger.info("=" * 60)

    # ── Redis Pub/Sub ────────────────────────────────────────────────

    async def publish_event(
        self,
        device_id: str,
        event_type: str,
        payload: dict,
    ) -> None:
        """Publish an event to Redis for App push notification.

        Channel: pigugu:events:{device_id}
        Consumed by FastAPI WebSocket Sync module.
        """
        message = json.dumps({
            "type": event_type,
            "payload": payload,
        }, ensure_ascii=False)

        if self._redis:
            try:
                await self._redis.publish(
                    f"pigugu:events:{device_id}", message
                )
                logger.info(f"📤 [PUBLISH] {event_type} → device={device_id}")
                return
            except Exception as e:
                logger.warning(f"Redis publish failed: {e}")

        # Log fallback
        logger.info(f"📤 [PUBLISH-OFF] {event_type}: {payload}")

    async def publish_conversation_end(
        self,
        device_id: str,
        state: "ConversationState",
        score: "ScoreResult",
    ) -> None:
        """Publish conversation_end event with full scoring data."""
        await self.publish_event(device_id, "conversation_end", {
            "session_id": state.session_id,
            "persona": state.persona_id,
            "mode": state.mode_id,
            "credibility": score.credibility,
            "roast_points": score.roast_points,
            "mood_delta": score.mood_delta,
            "mode_scores": score.mode_scores,
            "story_card": state.ending.story_card,
            "achievement_ids": score.achievement_ids,
        })

    async def publish_achievement(
        self, device_id: str, achievement_id: str
    ) -> None:
        """Publish achievement_unlocked event."""
        await self.publish_event(device_id, "achievement_unlocked", {
            "achievement_id": achievement_id,
        })
