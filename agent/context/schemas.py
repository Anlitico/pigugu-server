# agent/context/schemas.py
"""Data structures, Redis key schema, and PG DDL for context and memory.

4-Layer Context Architecture
─────────────────────────────
Layer 1 — System Prompt + Tools (~3-5K, prefix-cached, static per persona/mode)
Layer 2 — User Preference (~1-2K, prefix-cached, updated by background extraction)
Layer 3 — Session Context (dynamic: raw turns + tier-1/2 summaries, past roast summaries)
Layer 4 — Active Roast (transient, only when roast is in progress):
         4a: Roast prompt — RAW, never compressed (from PG, cached in Redis)
         4b: Roast turns — raw within token buffer, soft-compressed beyond buffer
         On roast end: extract user facts → Layer 2, conversation summary → Layer 3

Token Budget: 200K cap, dynamically allocated across layers at assembly time.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════════════════════
# Token Estimation (fallback when no provider token_counter is available)
# ═══════════════════════════════════════════════════════════════════════════════

def _len_fallback(text: str) -> int:
    """Character-length fallback when no provider token counter is available."""
    return len(text) if text else 0


# ═══════════════════════════════════════════════════════════════════════════════
# Token Budget
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TokenBudget:
    """Token allocation across the 4 context layers.

    Filled during assembly. Used to decide what to keep raw vs compress.
    """
    total_cap: int = 200_000

    # Actual usage per layer (set during assembly)
    layer_1_system: int = 0       # system prompt + tools
    layer_2_user_pref: int = 0    # user memory background
    layer_3_session: int = 0      # session summaries + raw turns
    layer_4_roast_prompt: int = 0 # roast prompt (4a, raw)
    layer_4_roast_turns: int = 0  # roast turns (4b)

    @property
    def used(self) -> int:
        return (
            self.layer_1_system +
            self.layer_2_user_pref +
            self.layer_3_session +
            self.layer_4_roast_prompt +
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


# ═══════════════════════════════════════════════════════════════════════════════
# Redis Key Schema — keyed by user_id
# ═══════════════════════════════════════════════════════════════════════════════

class RedisKeys:
    """Canonical Redis key patterns. All keyed by user_id."""

    # Layer 3 — Session
    @staticmethod
    def turns(user_id: str) -> str:
        return f"ctx:{user_id}:turns"

    @staticmethod
    def meta(user_id: str) -> str:
        return f"ctx:{user_id}:meta"

    @staticmethod
    def summary_recent(user_id: str) -> str:
        return f"ctx:{user_id}:summary:recent"

    @staticmethod
    def summary_global(user_id: str) -> str:
        return f"ctx:{user_id}:summary:global"

    @staticmethod
    def game_state(user_id: str) -> str:
        return f"ctx:{user_id}:game_state"

    @staticmethod
    def segments(user_id: str) -> str:
        return f"ctx:{user_id}:segments"

    @staticmethod
    def current_segment(user_id: str) -> str:
        return f"ctx:{user_id}:current_segment"

    # Layer 2 — User Memory
    @staticmethod
    def user_memory(user_id: str) -> str:
        return f"pigugu:user:{user_id}:memory"

    # Layer 4 — Active Roast
    @staticmethod
    def roast_prompt(user_id: str) -> str:
        """Roast prompt (4a). Cached from PG, RAW, read-only during session."""
        return f"ctx:{user_id}:roast:prompt"

    @staticmethod
    def roast_turns(user_id: str) -> str:
        """Roast turn buffer (4b). Separate list from session turns."""
        return f"ctx:{user_id}:roast:turns"

    @staticmethod
    def roast_summary(user_id: str) -> str:
        """Compressed summary of older roast turns (when buffer exceeded)."""
        return f"ctx:{user_id}:roast:summary"

    @staticmethod
    def roast_meta(user_id: str) -> str:
        """Roast-level metadata: token counts, buffer pressure, created_at."""
        return f"ctx:{user_id}:roast:meta"


# ═══════════════════════════════════════════════════════════════════════════════
# PG Table DDL
# ═══════════════════════════════════════════════════════════════════════════════

PG_CREATE_TURNS = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    turn_number     INT NOT NULL,
    role            TEXT NOT NULL,                -- system | user | assistant | tool
    content         TEXT NOT NULL DEFAULT '',
    tool_calls      JSONB DEFAULT NULL,           -- assistant: [{id, name, arguments}]
    tool_call_id    TEXT DEFAULT NULL,            -- tool: 关联的 call id
    name            TEXT DEFAULT NULL,            -- tool: 函数名
    partial         BOOLEAN DEFAULT FALSE,        -- assistant: 续写标记
    roast_id        TEXT DEFAULT NULL,            -- NULL = free chat
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, turn_number)
);
CREATE INDEX IF NOT EXISTS idx_turns_user
    ON conversation_turns(user_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_turns_roast
    ON conversation_turns(user_id, roast_id)
    WHERE roast_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_turns_tool_call
    ON conversation_turns(user_id, tool_call_id)
    WHERE tool_call_id IS NOT NULL;
"""

PG_CREATE_SUMMARIES = """
CREATE TABLE IF NOT EXISTS context_summaries (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    summary_type    TEXT NOT NULL,               -- 'session' (L3) | 'roast' (L4)
    roast_id        TEXT DEFAULT '',              -- roast 专属，session 为空
    tier            INT NOT NULL DEFAULT 1,       -- session: 1=recent, 2=global; roast: always 1
    summary         TEXT NOT NULL,
    start_turn      INT NOT NULL DEFAULT 0,       -- 覆盖的第一轮
    end_turn        INT NOT NULL DEFAULT 0,       -- 锚点：覆盖的最后一轮
    model_used      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_summaries_user
    ON context_summaries(user_id, summary_type, tier, end_turn DESC);
"""

PG_CREATE_USER_FACTS = """
CREATE TABLE IF NOT EXISTS user_facts (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    fact            TEXT NOT NULL,                    -- "Name: John" / "Prefers dark humor"
    category        TEXT NOT NULL DEFAULT 'personal', -- personal | preference | health | work | interest
    confidence      FLOAT DEFAULT 1.0,
    source_turn     INT DEFAULT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, fact)                            -- dedup by exact fact text
);
CREATE INDEX IF NOT EXISTS idx_facts_user
    ON user_facts(user_id, category);
"""

PG_CREATE_USER_MEMORY = """
CREATE TABLE IF NOT EXISTS user_memory (
    user_id         TEXT PRIMARY KEY,
    profile_summary TEXT NOT NULL DEFAULT '',   -- 用户画像，由 user_facts 汇总生成
    stats           JSONB DEFAULT '{}',
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
"""

PG_CREATE_ROAST_SCENARIOS = """
CREATE TABLE IF NOT EXISTS roast_scenarios (
    roast_id        TEXT PRIMARY KEY,                  -- "poison_2026-05-17_001"
    game_mode       TEXT NOT NULL,                     -- 'poison_opinion' | 'debate' | 'prediction' | 'breaking_bomb'
    prompt          TEXT NOT NULL,                     -- L4 context 注入文本（token-limited）
    news_id         TEXT DEFAULT '',                   -- 来源 post_id
    tags            JSONB DEFAULT '[]',                -- 分类标签
    status          TEXT NOT NULL DEFAULT 'active',    -- active | expired
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_roast_scenarios_mode
    ON roast_scenarios(game_mode, status);
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 2 — UserMemory
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class UserMemory:
    """Cross-session user profile. PG primary, Redis cache.

    Updated by background extraction job after each session ends.
    """

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


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 4 — RoastContext
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RoastContext:
    """Active roast data. Only exists while a roast is in progress.

    4a — prompt: RAW game rules/scenario, never compressed during the roast.
         Loaded from PG (written by news→roast cron job), cached in Redis.
    4b — turns: gameplay conversation turns. Raw within token buffer,
         soft-compressed into `summary` when buffer exceeded.

    On roast end: extract user facts → Layer 2, compress conversation → Layer 3,
    then discard the RoastContext.
    """

    roast_id: str
    prompt: str = ""               # 4a: RAW, from PG → Redis cache
    turns: list = field(default_factory=list)  # 4b: list[Message], raw within buffer
    summary: str = ""              # 4b: compressed older turns (when over buffer)

    # Token counts (set during assembly)
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


# ═══════════════════════════════════════════════════════════════════════════════
# ContextSegment — historical segment tracking (for closed roasts)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ContextSegment:
    """A closed conversation segment. Stored in Redis for cross-roast context.

    Active roasts are tracked via RoastContext. When a roast ends, a
    ContextSegment record is created with its compressed summary.
    """

    roast_id: str = ""
    start_turn: int = 0
    end_turn: int = 0
    status: str = "active"          # active | closed
    summary: str = ""               # compressed after closure

    def to_dict(self) -> dict:
        return {
            "roast_id": self.roast_id,
            "start_turn": self.start_turn,
            "end_turn": self.end_turn,
            "status": self.status,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ContextSegment":
        return cls(
            roast_id=d.get("roast_id", ""),
            start_turn=int(d.get("start_turn", 0)),
            end_turn=int(d.get("end_turn", 0)),
            status=d.get("status", "active"),
            summary=d.get("summary", ""),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Tool Call Completeness Validation
# ═══════════════════════════════════════════════════════════════════════════════

def validate_tool_calls(messages: list) -> list:
    """Filter out incomplete tool calls before sending to LLM.

    LangGraph-style: every assistant tool_call must have a matching tool
    response with the same tool_call_id. Dangling calls (no response yet)
    are removed to avoid LLM API errors.

    Also enforces alternating pattern: tool messages must follow an
    assistant with tool_calls, and tool responses must match pending calls.
    """
    if not messages:
        return messages

    from core.llm.types import Message

    # Collect all tool_call_ids from tool messages
    responded_ids: set[str] = set()
    for m in messages:
        if m.role == "tool" and m.tool_call_id:
            responded_ids.add(m.tool_call_id)

    cleaned: list[Message] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            # Keep only tool_calls that have a matching tool response
            valid_calls = [
                tc for tc in m.tool_calls
                if tc.id in responded_ids
            ]
            if valid_calls:
                # Replace with a copy that only has resolved calls
                cleaned.append(Message(
                    role="assistant",
                    content=m.content,
                    tool_calls=valid_calls,
                    partial=m.partial,
                ))
            elif m.content:
                # No resolved calls, but has text content → keep text only
                cleaned.append(Message(
                    role="assistant",
                    content=m.content,
                    partial=m.partial,
                ))
            # else: no content and no resolved calls → drop entirely
        else:
            cleaned.append(m)

    return cleaned


# ═══════════════════════════════════════════════════════════════════════════════
# WorkingContext — assembled LLM-visible context with 4-layer structure
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class WorkingContext:
    """Per-user LLM-visible context. Hot-path assembled from Redis < 5ms.

    Layers:
      L1 — system_prompt (passed separately, not stored here)
      L2 — user_memory: cross-session profile
      L3 — raw_turns + recent_summary + global_summary: session context
      L4 — roast: active game context (None if free chat)
    """

    user_id: str

    # Layer 3 — Session
    raw_turns: list = field(default_factory=list)
    recent_summary: str = ""
    global_summary: str = ""
    tier: int = 0
    game_state: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    # Layer 4 — Active Roast (None when free chat)
    roast: RoastContext | None = None

    # Layer 2 — User Memory
    user_memory: UserMemory | None = None

    # Budget (filled during assembly)
    budget: TokenBudget = field(default_factory=TokenBudget)

    # ── 4-Layer Message Assembly ──────────────────────────────────────

    def to_messages(
        self,
        system_prompt: str = "",
        previous_segments: list[str] | None = None,
        *,
        token_counter=None,
    ) -> list:
        """Assemble into LLM-ready Message list following 4-layer order.

        token_counter: callable(text) -> int, from provider.count_tokens.
                       Falls back to len() if not provided.

        Order:
          1. [system] Layer 1: system prompt (persona + tools)
          2. [system] Layer 2: user background
          3. [system] Layer 3: global summary (tier-2)
          4. [system] Layer 3: recent summary (tier-1)
          5. [system] Previous closed segments (cross-roast context)
          ─── all system messages above this line ───
          6. [user/as] Layer 3: session raw turns (oldest first → newest last)
          7. [user]   Layer 4: roast summary (includes roast prompt + gameplay summary)
          8. [user/as] Layer 4: roast raw turns (newest last)

        When L3 is not compressed, session raw turns include all turns
        (free chat happened before the roast). Chronologically correct.
        """
        from core.llm.types import Message

        tc = token_counter or _len_fallback

        result: list[Message] = []
        budget = self.budget

        # Layer 1: System prompt
        if system_prompt:
            result.append(Message.system(system_prompt))
            budget.layer_1_system = tc(system_prompt)

        # Layer 2: User background
        if self.user_memory and self.user_memory.profile_summary:
            text = f"[User profile]\n{self.user_memory.profile_summary}"
            result.append(Message.system(text))
            budget.layer_2_user_pref = tc(text)

        # Layer 3: Global summary (tier-2, earlier conversation)
        if self.tier >= 2 and self.global_summary:
            text = f"[Earlier conversation]\n{self.global_summary}"
            result.append(Message.system(text))

        # Layer 3: Recent summary (tier-1)
        if self.tier >= 1 and self.recent_summary:
            text = f"[Recent conversation]\n{self.recent_summary}"
            result.append(Message.system(text))

        # Previous closed segments (cross-roast context)
        if previous_segments:
            for summary in previous_segments[:3]:
                if summary:
                    text = f"[Previous game session]\n{summary}"
                    result.append(Message.system(text))

        # ── System / Conversation boundary ──

        # Layer 3: Session raw turns (oldest first → newest last)
        # Free chat turns that happened before or outside the roast
        for turn in reversed(self.raw_turns):
            result.append(turn)

        # Layer 4: Roast summary as user message (includes roast prompt + gameplay summary)
        if self.roast and self.roast.summary:
            text = f"[Game scenario + history]\n{self.roast.summary}"
            result.append(Message.user(text))
            budget.layer_4_roast_prompt = tc(text)

        # Layer 4: Roast raw turns (newest last)
        if self.roast and self.roast.turns:
            for turn in reversed(self.roast.turns):
                result.append(turn)
            budget.layer_4_roast_turns = sum(tc(t.content) for t in self.roast.turns)

        # Track budget for L3
        l3_system = sum(tc(m.content) for m in result if m.role == "system") - budget.layer_1_system - budget.layer_2_user_pref
        budget.layer_3_session = l3_system

        return validate_tool_calls(result)

    def budget_summary(self) -> dict:
        """Return human-readable budget breakdown for logging."""
        return {
            "total_cap": self.budget.total_cap,
            "used": self.budget.used,
            "remaining": self.budget.remaining,
            "breakdown": {
                "L1_system": self.budget.layer_1_system,
                "L2_user_pref": self.budget.layer_2_user_pref,
                "L3_session": self.budget.layer_3_session,
                "L4_roast_prompt": self.budget.layer_4_roast_prompt,
                "L4_roast_turns": self.budget.layer_4_roast_turns,
            },
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

RAW_TURN_COUNT = 5           # session raw turns kept visible
ROAST_RAW_TURN_COUNT = 10    # roast raw turns kept visible before compression
HOT_WINDOW_SIZE = 100        # max Redis list length for turns
COMPRESSION_THRESHOLD = 20   # turns before triggering compression
FLUSH_BATCH_SIZE = 10
FLUSH_INTERVAL_SECONDS = 5
TOKEN_BUDGET_CAP = 200_000   # context window cap
ROAST_BUFFER_RATIO = 0.6     # max fraction of remaining budget for roast turns
USER_FACT_EXTRACT_COUNT = 15 # max extracted facts per extraction run

# Redis meta field names (canonical, avoid string literals in manager)
META_ANCHOR = "compression_anchor"
META_ANCHOR_IN_PROGRESS = "compression_in_progress"
META_TIER = "compressed_tier"
META_TURN_COUNT = "turn_count"
META_LAST_COMPRESSED = "last_compressed_at"
META_ROAST_ID = "roast_id"
