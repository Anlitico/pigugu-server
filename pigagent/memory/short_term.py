# pigagent/memory/short_term.py
"""ShortTermMemory — wraps in-process session history."""

from collections import defaultdict
from .store import MemoryStore


class ShortTermMemory(MemoryStore):
    """In-process short-term memory for current conversation session.

    Wraps session-scoped turn storage. Not persistent across restarts.
    For production with LiveKit, the ChatContext is the canonical store;
    this class provides a programmatic API on top of it.
    """

    def __init__(self):
        self._turns: dict[str, list[dict]] = defaultdict(list)
        self._facts: dict[str, list[str]] = defaultdict(list)

    # ── Short-term ──────────────────────────────────────────────────

    def add_turn(self, user_id: str, role: str, content: str) -> None:
        self._turns[user_id].append({
            "role": role,
            "content": content,
        })

    def get_recent(self, user_id: str, n: int = 10) -> list[dict]:
        turns = self._turns.get(user_id, [])
        return turns[-n:] if n > 0 else turns

    def get_summary(self, user_id: str) -> str:
        turns = self._turns.get(user_id, [])
        if not turns:
            return ""

        user_turns = [t for t in turns if t["role"] == "user"]
        assistant_turns = [t for t in turns if t["role"] == "assistant"]

        lines = []
        if len(turns) > 2:
            lines.append(f"Conversation so far: {len(turns)} messages exchanged.")
        if user_turns:
            last_user = user_turns[-1]["content"]
            lines.append(f"Last thing user said: '{last_user[:120]}'")

        # Add long-term facts if available
        facts = self._facts.get(user_id, [])
        if facts:
            lines.append("Known about this user:")
            for f in facts[-5:]:
                lines.append(f"  - {f}")

        return "\n".join(lines)

    # ── Long-term (in-process for now; will move to LongTermMemory) ─

    async def add_fact(self, user_id: str, fact: str) -> None:
        self._facts[user_id].append(fact)

    async def get_facts(self, user_id: str) -> list[str]:
        return self._facts.get(user_id, [])

    async def search(self, user_id: str, query: str, limit: int = 5) -> list[str]:
        # Simple keyword match for in-process mode
        # Production: replace with vector search
        facts = self._facts.get(user_id, [])
        query_lower = query.lower()
        matches = [f for f in facts if any(
            word in f.lower() for word in query_lower.split()
        )]
        return matches[:limit]
