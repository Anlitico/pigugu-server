# pigagent/memory/store.py
"""
MemoryStore abstract base class.

Short-term = current conversation turns (ChatContext wrapper).
Long-term = cross-session user facts and preferences.
"""

from abc import ABC, abstractmethod
from typing import Optional


class MemoryStore(ABC):
    """Interface for the two-tier memory system.

    Short-term memory is scoped to the current session and provides
    the last N turns for context injection.

    Long-term memory persists across sessions (user preferences,
    interaction history, learned facts).
    """

    # ── Short-term (current session) ────────────────────────────────

    @abstractmethod
    def add_turn(self, user_id: str, role: str, content: str) -> None:
        """Record a conversation turn in short-term memory."""

    @abstractmethod
    def get_recent(self, user_id: str, n: int = 10) -> list[dict]:
        """Get the most recent N turns."""

    @abstractmethod
    def get_summary(self, user_id: str) -> str:
        """Condensed memory summary for system prompt injection.

        Returns a string like:
        "Previous interactions: user prefers short responses,
         disagreed on tariffs, knowledgeable about tech."
        """

    # ── Long-term (cross-session) ───────────────────────────────────

    @abstractmethod
    async def add_fact(self, user_id: str, fact: str) -> None:
        """Store a persistent fact about the user."""

    @abstractmethod
    async def get_facts(self, user_id: str) -> list[str]:
        """Retrieve all stored facts for a user."""

    @abstractmethod
    async def search(self, user_id: str, query: str, limit: int = 5) -> list[str]:
        """Semantic search over stored memories."""
