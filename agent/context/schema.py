# agent/context/schemas.py
"""Core data structures for the 4-layer agent context architecture.

Layer 1 — System Prompt + Tools (~3-5K, prefix-cached)
Layer 2 — User Preference (~1-2K, prefix-cached)
Layer 3 — Session Context (dynamic: raw turns + summaries)
Layer 4 — Active Roast (transient: prompt → summary → raw turns)

Token Budget: 200K cap, dynamically allocated at assembly time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.llm.types import Message

from .validation import _len_fallback, validate_tool_calls


@dataclass
class ConversationRecord:
    """A stored turn — Redis/PG intermediate between Message and AgentConversation.

    turn_number is the global counter. roast_id and roast_start are embedded
    in the data so assembly can determine boundaries without reading meta.
    """

    turn_number: int
    role: str
    content: str
    roast_id: str | None = None       # None = free chat
    roast_start: bool = False          # true = this turn is the roast prompt

    @classmethod
    def from_message(cls, turn_number: int, msg: Message, *, roast_id: str | None = None, roast_start: bool = False) -> "ConversationRecord":
        return cls(turn_number=turn_number, role=msg.role, content=msg.content, roast_id=roast_id, roast_start=roast_start)

    def to_message(self) -> Message:
        return Message(role=self.role, content=self.content)

    def to_dict(self) -> dict:
        d = {"turn": self.turn_number, "role": self.role, "content": self.content}
        if self.roast_id:
            d["roast_id"] = self.roast_id
        if self.roast_start:
            d["roast_start"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationRecord":
        return cls(
            turn_number=d["turn"],
            role=d["role"],
            content=d["content"],
            roast_id=d.get("roast_id"),
            roast_start=d.get("roast_start", False),
        )


@dataclass
class SummaryRecord:
    """A stored summary with embedded position info.

    end_turn anchors the summary to the turn timeline — all turns ≤ end_turn
    are covered by this summary. tier distinguishes recent (1) from global (2).
    """

    text: str
    end_turn: int = 0
    tier: int = 1     # 1=recent, 2=global

    def serialize(self) -> str:
        import json
        return json.dumps({"text": self.text, "end_turn": self.end_turn, "tier": self.tier}, ensure_ascii=False)

    @classmethod
    def deserialize(cls, raw: str) -> "SummaryRecord":
        import json
        try:
            data = json.loads(raw)
            return cls(text=data["text"], end_turn=data["end_turn"], tier=data["tier"])
        except Exception:
            return cls(text=raw)


@dataclass
class TokenBudget:
    """Token allocation across the 4 context layers."""

    total_cap: int = 200_000  # default; override from config.CONTEXT_TOKEN_BUDGET_CAP at assembly time
    layer_1_system: int = 0
    layer_2_user_pref: int = 0
    layer_3_session: int = 0
    layer_4_roast_prompt: int = 0
    layer_4_roast_turns: int = 0

    @property
    def used(self) -> int:
        return (
            self.layer_1_system + self.layer_2_user_pref +
            self.layer_3_session + self.layer_4_roast_prompt +
            self.layer_4_roast_turns
        )

    @property
    def remaining(self) -> int:
        return max(0, self.total_cap - self.used)

    def to_dict(self) -> dict:
        return {
            "total_cap": self.total_cap,
            "layer_1_system": self.layer_1_system,
            "layer_2_user_pref": self.layer_2_user_pref,
            "layer_3_session": self.layer_3_session,
            "layer_4_roast_prompt": self.layer_4_roast_prompt,
            "layer_4_roast_turns": self.layer_4_roast_turns,
            "used": self.used,
            "remaining": self.remaining,
        }


@dataclass
class UserMemory:
    """Cross-session user profile. PG primary, Redis cache."""

    user_id: str
    profile_summary: str = ""
    stats: dict = field(default_factory=dict)

    def to_hash(self) -> dict:
        import json
        return {
            "profile_summary": self.profile_summary,
            "stats_json": json.dumps(self.stats, ensure_ascii=False),
        }

    @classmethod
    def from_hash(cls, h: dict) -> "UserMemory":
        import json
        user_id = h.get("user_id", "")
        return cls(
            user_id=user_id,
            profile_summary=h.get("profile_summary", ""),
            stats=json.loads(h.get("stats_json", "{}")),
        )

    def token_count(self, token_counter=None) -> int:
        counter = token_counter or _len_fallback
        return counter(self.profile_summary)


@dataclass
class RoastContext:
    """Active roast data. Only exists while a roast is in progress.

    4a — prompt: RAW game rules, never compressed during the roast.
    4b — turns: gameplay turns, raw within buffer, soft-compressed when exceeded.
    On roast end: extract facts → L2, compress → L3, then discard.
    """

    roast_id: str
    prompt: str = ""
    turns: list = field(default_factory=list)
    summary: str = ""
    prompt_tokens: int = 0
    turns_tokens: int = 0
    summary_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.turns_tokens + self.summary_tokens

    @property
    def is_active(self) -> bool:
        return bool(self.roast_id)

    def to_meta(self) -> dict:
        return {
            "roast_id": self.roast_id,
            "prompt_tokens": self.prompt_tokens,
            "turns_tokens": self.turns_tokens,
            "summary_tokens": self.summary_tokens,
            "turn_count": len(self.turns),
        }


@dataclass
class WorkingContext:
    """Per-user LLM-visible context. Hot-path assembled from Redis < 5ms.

    L3 — single recursive summary (anchor via SummaryRecord.end_turn)
    L4 — roast context (only if active roast)
    L2 — user memory profile
    """

    user_id: str

    # L3 — Session
    raw_turns: list = field(default_factory=list)
    summary: str = ""                # recursive conversation summary
    summary_end_turn: int = 0        # anchor: all turns ≤ this are covered
    game_state: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    # L4 — Active Roast
    roast: RoastContext | None = None

    # L2 — User Memory
    user_memory: UserMemory | None = None

    # Budget
    budget: TokenBudget = field(default_factory=TokenBudget)

    def to_messages(self, *, system_prompt: str = "", token_counter=None) -> list:
        """Unified anchor assembly.

        ── system area (≤ anchor) ──
        L1 + L2 + L3 summary + L4 roast summary
        ── conversation area (> anchor) ──
        all raw turns
        """
        from core.llm.types import Message

        tc = token_counter or _len_fallback
        result: list[Message] = []
        budget = self.budget

        if system_prompt:
            result.append(Message.system(system_prompt))
            budget.layer_1_system = tc(system_prompt)

        if self.user_memory and self.user_memory.profile_summary:
            result.append(Message.system(f"[User profile]\n{self.user_memory.profile_summary}"))
            budget.layer_2_user_pref = tc(self.user_memory.profile_summary)

        if self.summary:
            result.append(Message.system(f"[Conversation history]\n{self.summary}"))
            budget.layer_3_session = tc(self.summary)

        if self.roast and self.roast.summary:
            result.append(Message.system(f"[Game scenario + history]\n{self.roast.summary}"))
            budget.layer_4_roast_prompt = tc(self.roast.summary)

        for turn in reversed(self.raw_turns):
            if isinstance(turn, ConversationRecord):
                result.append(turn.to_message())
            else:
                result.append(turn)

        return validate_tool_calls(result)

    def budget_summary(self) -> dict:
        return {
            "total_cap": self.budget.total_cap,
            "used": self.budget.used,
            "remaining": self.budget.remaining,
            "breakdown": {
                "L1_system": self.budget.layer_1_system,
                "L2_user_pref": self.budget.layer_2_user_pref,
                "L3_session": self.budget.layer_3_session,
                "L4_roast_prompt": self.budget.layer_4_roast_prompt,
            },
        }
