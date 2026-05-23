# pigagent/memory/long_term.py
"""LongTermMemory — cross-session user memory (Redis + PostgreSQL)."""

from typing import Optional

from loguru import logger

from .store import MemoryStore


class LongTermMemory(MemoryStore):
    """Persistent long-term memory backed by Redis (cache) + PostgreSQL (durable).

    Redis keys:
        pigugu:user:{user_id}:facts  → list of fact strings
        pigugu:user:{user_id}:summary → condensed summary string

    PostgreSQL table:
        user_memory (user_id, fact, category, created_at, expires_at)

    For Phase 4, falls back to in-process storage when Redis/PG are unavailable.
    """

    def __init__(
        self,
        redis_client=None,
        pg_pool=None,
    ):
        self._redis = redis_client
        self._pg = pg_pool
        # In-process fallback
        self._facts: dict[str, list[str]] = {}
        self._summaries: dict[str, str] = {}

    # ── Short-term (delegated to in-process) ────────────────────────

    def add_turn(self, user_id: str, role: str, content: str) -> None:
        # Long-term memory doesn't store individual turns.
        # Turns are handled by ShortTermMemory.
        pass

    def get_recent(self, user_id: str, n: int = 10) -> list[dict]:
        return []

    def get_summary(self, user_id: str) -> str:
        return self._summaries.get(user_id, "")

    # ── Long-term ───────────────────────────────────────────────────

    async def add_fact(self, user_id: str, fact: str) -> None:
        """Store a fact about the user. Async — does not block speech."""
        if self._redis:
            try:
                await self._redis.rpush(
                    f"pigugu:user:{user_id}:facts", fact
                )
                return
            except Exception as e:
                logger.warning(f"Redis write failed, using local fallback: {e}")

        # In-process fallback
        self._facts.setdefault(user_id, []).append(fact)

    async def get_facts(self, user_id: str) -> list[str]:
        """Retrieve all stored facts."""
        if self._redis:
            try:
                facts = await self._redis.lrange(
                    f"pigugu:user:{user_id}:facts", 0, -1
                )
                return [f.decode() if isinstance(f, bytes) else f for f in facts]
            except Exception as e:
                logger.warning(f"Redis read failed, using local fallback: {e}")

        return self._facts.get(user_id, [])

    async def search(self, user_id: str, query: str, limit: int = 5) -> list[str]:
        """Keyword search over stored facts.

        Production: replace with pgvector semantic search.
        """
        facts = await self.get_facts(user_id)
        query_lower = query.lower()
        matches = [
            f for f in facts
            if any(word in f.lower() for word in query_lower.split())
        ]
        return matches[:limit]

    async def update_summary(self, user_id: str, summary: str) -> None:
        """Update the condensed user summary."""
        if self._redis:
            try:
                await self._redis.set(
                    f"pigugu:user:{user_id}:summary", summary
                )
                return
            except Exception as e:
                logger.warning(f"Redis write failed: {e}")

        self._summaries[user_id] = summary
