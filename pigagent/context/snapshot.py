# pigagent/context/snapshot.py
"""ContextSnapshot  -  wraps list[ConversationRecord] with token counting,
segment splitting, and compression eligibility checks.

Used by manager.assemble() and ContextCompressor.
"""

from __future__ import annotations

from config import get_config

_cfg = get_config()

from core.llm import get_llm
from core.llm.types import Message
from context.schema import ConversationRecord
from context.roast import RoastState


class ContextSnapshot:
    """Point-in-time snapshot of one user's hot conversation records.

    All token counting and segment queries live here  -  a single source of
    truth for "what does this user's conversation look like right now?"
    """

    def __init__(self, records: list[ConversationRecord]):
        self.records = records

    # ── Token Counting ──────────────────────────────────────────────

    async def token_count(self, *, model: str = "qwen-plus") -> int:
        """Total tokens across all records in this snapshot."""
        if not self.records:
            return 0
        provider = get_llm(model)
        return await provider.count_tokens([r.to_message() for r in self.records])

    async def token_count_with_summary(
        self, *, l3_summary: str = "", l4_summary: str = "", model: str = "qwen-plus",
    ) -> int:
        """Total tokens that would enter the LLM: L3 + L4 + raw records."""
        total = await self.token_count(model=model)
        provider = get_llm(model)
        if l3_summary:
            total += await provider.count_tokens(l3_summary)
        if l4_summary:
            total += await provider.count_tokens(l4_summary)
        return total

    async def token_count_roast(self, *, model: str = "qwen-plus") -> int:
        """Tokens in the roast segment only."""
        roast = self.roast
        if not roast:
            return 0
        provider = get_llm(model)
        return await provider.count_tokens([r.to_message() for r in roast])

    # ── Roast Analysis ──────────────────────────────────────────────

    @property
    def roast_start_idx(self) -> int | None:
        """Index of the first record with a roast_instance_id, or None."""
        for i, r in enumerate(self.records):
            if r.roast_instance_id:
                return i
        return None

    @property
    def scenario(self) -> str:
        """'roast' if the most recent record has an active roast_instance_id."""
        return "roast" if RoastState.is_active(self.records) else "free_chat"

    @property
    def roast_instance_id(self) -> str:
        """Current roast_instance_id, or empty string."""
        return RoastState.current_roast_instance_id(self.records) or ""

    @property
    def pre_roast(self) -> list[ConversationRecord]:
        """Records before the roast boundary (all if no roast)."""
        idx = self.roast_start_idx
        if idx is None:
            return self.records
        return self.records[:idx]

    @property
    def roast(self) -> list[ConversationRecord]:
        """Records from the roast boundary onward (empty if no roast)."""
        idx = self.roast_start_idx
        if idx is None:
            return []
        return self.records[idx:]

    def split(self) -> tuple[list[ConversationRecord], list[ConversationRecord]]:
        """Returns (pre_roast, roast)."""
        return self.pre_roast, self.roast

    # ── Compression Triggers ────────────────────────────────────────

    async def should_compress(
        self, *, existing_summary: str = "", model: str = "qwen-plus",
    ) -> bool:
        """True if compression should run.

        Primary: total tokens (summary + records) > budget cap.
        Backup:  too many uncompressed turns (> max_turns).
        """
        total = await self.token_count_with_summary(l3_summary=existing_summary, model=model)
        if total > _cfg.CONTEXT_TOKEN_BUDGET_CAP:
            return True
        if len(self.records) > _cfg.CONTEXT_MAX_TURNS:
            return True
        return False

    async def should_compress_l4(self, *, model: str = "qwen-plus") -> bool:
        """True if roast segment has enough tokens to justify L4 compression."""
        roast_tokens = await self.token_count_roast(model=model)
        threshold = max(
            int(_cfg.CONTEXT_TOKEN_BUDGET_CAP * _cfg.CONTEXT_ROAST_COMPRESSION_RATIO),
            _cfg.CONTEXT_ROAST_COMPRESSION_MIN_TOKENS,
        )
        return roast_tokens > threshold

    # ── Conversion ──────────────────────────────────────────────────

    def to_messages(self, records: list[ConversationRecord]) -> list[Message]:
        """Convert a subset of records to Message list."""
        return [r.to_message() for r in records]
