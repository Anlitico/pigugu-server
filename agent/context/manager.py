# agent/context/manager.py
"""ContextManager — per-user conversation context with hot/cold storage.

Keyed by user_id. One user = one continuous session.

Compression uses async anchors: before an LLM compression call, the current
turn_count is recorded as compression_anchor. New turns arriving during
compression have turn_number > anchor. On assembly, raw turns are filtered
to only those turn_number > anchor — the summary covers everything ≤ anchor.

Roast-aware gating: L2/L3 compression/extraction only fires during free chat,
never during an active roast. During a roast, only L4 (roast-internal)
compression fires, and only when token budget is under pressure.
"""

from __future__ import annotations

import asyncio
import json
import time

from loguru import logger

from core.llm.types import Message

from .schemas import (
    RedisKeys, UserMemory, ContextSegment, RoastContext, WorkingContext,
    RAW_TURN_COUNT, ROAST_RAW_TURN_COUNT, HOT_WINDOW_SIZE,
    COMPRESSION_THRESHOLD, FLUSH_BATCH_SIZE, FLUSH_INTERVAL_SECONDS,
    ROAST_BUFFER_RATIO, USER_FACT_EXTRACT_COUNT,
    META_ANCHOR, META_ANCHOR_IN_PROGRESS, META_TIER, META_TURN_COUNT,
    META_LAST_COMPRESSED, META_ROAST_ID,
)
from .compression import ContextCompressor


def _serialize_tool_calls(tool_calls: list | None) -> str | None:
    """Serialize ToolCall list to JSONB string for PG insert. Returns None if empty."""
    if not tool_calls:
        return None
    import json
    return json.dumps([
        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
        for tc in tool_calls
    ])


class ContextManager:
    """Per-user conversation context with hot/cold storage.

    Not meant to be used directly — use ContextLoader instead.
    """

    def __init__(
        self,
        user_id: str,
        redis_client=None,
        pg_pool=None,
    ):
        self._user_id = user_id
        self._redis = redis_client
        self._pg = pg_pool
        self._compressor = ContextCompressor()

        self._pg_buffer: list[tuple[int, Message, str | None]] = []
        self._pg_lock = asyncio.Lock()
        self._compression_in_flight = False
        self._flush_started = False
        self._user_memory: UserMemory | None = None

    # ── Public Properties ─────────────────────────────────────────────

    @property
    def user_memory(self) -> UserMemory:
        if self._user_memory is None:
            self._user_memory = UserMemory(user_id=self._user_id)
        return self._user_memory

    # ── Session Lifecycle ─────────────────────────────────────────────

    async def init_session(self, roast_id: str) -> None:
        """Initialize the user session. No-op if already exists."""
        mode_id = ""
        persona_id = ""
        news_id = ""
        exists = False
        if self._redis:
            try:
                exists = await self._redis.exists(
                    RedisKeys.meta(self._user_id)
                ) > 0
            except Exception:
                pass

        if not exists and self._redis:
            await self._redis.hset(
                RedisKeys.meta(self._user_id),
                mapping={
                    META_TURN_COUNT: 0,
                    META_TIER: 0,
                    META_ROAST_ID: roast_id,
                    "mode_id": mode_id,
                    "persona_id": persona_id,
                    "news_id": news_id,
                    META_ANCHOR: 0,
                    META_ANCHOR_IN_PROGRESS: "0",
                },
            )
            await self._redis.expire(RedisKeys.turns(self._user_id), 86400)
            await self._load_user_memory()

            logger.info(
                f"[Context] Initialized for user={self._user_id}, "
                f"roast={roast_id}"
            )

    async def end_roast(self) -> None:
        """End the current roast, clean up roast Redis keys, return to free chat.

        Context is continuous — there is no 'session close'. Roast compression
        and L2 extraction happen incrementally during normal operation.
        This method only handles roast-specific teardown.
        """
        try:
            await self._flush_pg_buffer()

            if self._redis:
                meta = await self._read_meta()
                roast_id = meta.get(META_ROAST_ID, "")
                if roast_id:
                    # Clear roast state from Redis meta
                    await self._redis.hset(
                        RedisKeys.meta(self._user_id),
                        mapping={
                            META_ROAST_ID: "",
                            META_ANCHOR: 0,
                            META_ANCHOR_IN_PROGRESS: "0",
                        },
                    )
                    # Clean up roast-specific Redis keys
                    await self._redis.delete(
                        RedisKeys.roast_prompt(self._user_id),
                        RedisKeys.roast_turns(self._user_id),
                        RedisKeys.roast_summary(self._user_id),
                        RedisKeys.roast_meta(self._user_id),
                    )
                    logger.info(
                        f"[Context] Roast ended for user={self._user_id}, "
                        f"roast={roast_id}"
                    )

        except Exception as e:
            logger.error(f"[Context] End roast failed for {self._user_id}: {e}")

    # ── Turn Recording ────────────────────────────────────────────────

    async def add_turn(self, role: str, content: str) -> None:
        """Record a turn. Redis LPUSH (<1ms), async PG batch later.

        If Redis turn_count is 0 (cold start or data loss), recovers
        from PG MAX(turn_number) before incrementing.
        """
        meta = await self._read_meta()
        current = int(meta.get(META_TURN_COUNT, 0))

        # Recover from PG if Redis counter is uninitialized or lost
        if current == 0:
            current = await self._recover_turn_counter()
            if self._redis:
                await self._redis.hset(
                    RedisKeys.meta(self._user_id), META_TURN_COUNT, current
                )

        turn_count = current + 1
        turn = Message(role=role, content=content)

        # Redis: store turn_number in the JSON wrapper (not on Message)
        data = json.dumps({"turn": turn_count, **turn.to_dict()}, ensure_ascii=False)

        if self._redis:
            try:
                async with self._redis.pipeline() as pipe:
                    pipe.lpush(RedisKeys.turns(self._user_id), data)
                    pipe.ltrim(
                        RedisKeys.turns(self._user_id), 0, HOT_WINDOW_SIZE - 1
                    )
                    pipe.hincrby(RedisKeys.meta(self._user_id), META_TURN_COUNT, 1)
                    await pipe.execute()
            except Exception as e:
                logger.warning(f"Redis write failed: {e}")

        roast_id = meta.get(META_ROAST_ID, "") or None

        if self._pg:
            async with self._pg_lock:
                self._pg_buffer.append((turn_count, turn, roast_id))
                if len(self._pg_buffer) >= FLUSH_BATCH_SIZE:
                    asyncio.create_task(self._flush_pg_buffer())
                self._start_flush_timer()

    async def flush(self) -> None:
        """Force-flush PG buffer."""
        await self._flush_pg_buffer()

    # ── Context Assembly ──────────────────────────────────────────────

    async def assemble(self) -> WorkingContext:
        """Build WorkingContext from Redis. < 5ms hot path.

        Uses compression_anchor to split raw turns from summary-covered turns.
        Gates L3 compression on "no active roast". Triggers L4 compression
        if roast token budget is under pressure.
        """
        meta = await self._read_meta()
        tier = int(meta.get(META_TIER, 0))
        anchor = int(meta.get(META_ANCHOR, 0))
        roast_id = meta.get(META_ROAST_ID, "")

        wc = WorkingContext(
            user_id=self._user_id,
            tier=tier,
            game_state=await self._read_game_state(),
            meta=meta,
            user_memory=self._user_memory,
        )

        if tier >= 2:
            wc.global_summary = await self._read_summary("global")
        if tier >= 1:
            wc.recent_summary = await self._read_summary("recent")

        # Raw turns: only those AFTER the compression anchor (not yet summarized)
        wc.raw_turns = await self._get_hot_turns(RAW_TURN_COUNT, after_anchor=anchor)

        # Layer 4: load active roast context if a roast is in progress
        if roast_id:
            wc.roast = await self._load_roast_context(roast_id)

        # Compression triggers (fire-and-forget, async)
        if not self._compression_in_flight:
            in_progress = meta.get(META_ANCHOR_IN_PROGRESS, "0") == "1"

            if roast_id and wc.roast:
                # Active roast → only L4 compression
                if not in_progress and self._roast_budget_pressured(wc.roast):
                    self._schedule_roast_compression()
            else:
                # Free chat → L3 compression
                turn_count = int(meta.get(META_TURN_COUNT, 0))
                if not in_progress and turn_count >= COMPRESSION_THRESHOLD:
                    self._schedule_compression()

        return wc

    # ── Layer 4: Roast Context ────────────────────────────────────────

    async def _load_roast_context(self, roast_id: str) -> RoastContext:
        """Load active roast data from Redis."""
        rc = RoastContext(roast_id=roast_id)

        if self._redis:
            try:
                prompt_raw = await self._redis.get(
                    RedisKeys.roast_prompt(self._user_id)
                )
                if prompt_raw:
                    rc.prompt = prompt_raw.decode() if isinstance(prompt_raw, bytes) else prompt_raw
                    rc.prompt_tokens = len(rc.prompt) // 3

                # Read roast meta for anchor
                roast_meta_raw = await self._redis.hgetall(
                    RedisKeys.roast_meta(self._user_id)
                )
                roast_meta = {}
                if roast_meta_raw:
                    roast_meta = {
                        (k.decode() if isinstance(k, bytes) else k):
                        (v.decode() if isinstance(v, bytes) else v)
                        for k, v in roast_meta_raw.items()
                    }
                roast_anchor = int(roast_meta.get(META_ANCHOR, 0))

                roast_turns_raw = await self._redis.lrange(
                    RedisKeys.roast_turns(self._user_id), 0, -1
                )
                if roast_turns_raw:
                    all_roast_turns = []
                    for t in reversed(roast_turns_raw):
                        d = json.loads(t.decode() if isinstance(t, bytes) else t)
                        turn_num = d.pop("turn", 0)
                        msg = Message.from_dict(d)
                        # Filter: only turns after roast anchor (not yet summarized)
                        if turn_num > roast_anchor:
                            all_roast_turns.append(msg)
                    rc.turns = all_roast_turns

                summary_raw = await self._redis.get(
                    RedisKeys.roast_summary(self._user_id)
                )
                if summary_raw:
                    rc.summary = summary_raw.decode() if isinstance(summary_raw, bytes) else summary_raw
                    rc.summary_tokens = len(rc.summary) // 3

            except Exception as e:
                logger.warning(f"Failed to load roast context: {e}")

        return rc

    @staticmethod
    def _roast_budget_pressured(roast: RoastContext) -> bool:
        """Check if roast turns are consuming too much of their token budget."""
        if not roast.turns:
            return False
        # Simple heuristic: more than ROAST_RAW_TURN_COUNT turns → trigger
        return len(roast.turns) > ROAST_RAW_TURN_COUNT

    def _schedule_roast_compression(self) -> None:
        """Fire-and-forget L4 roast compression."""
        self._compression_in_flight = True
        asyncio.create_task(self._compress_roast())

    async def _compress_roast(self) -> None:
        """L4: Compress oldest roast turns into roast_summary (incremental)."""
        try:
            roast_meta_raw = await self._redis.hgetall(
                RedisKeys.roast_meta(self._user_id)
            ) if self._redis else {}
            roast_meta = {}
            if roast_meta_raw:
                roast_meta = {
                    (k.decode() if isinstance(k, bytes) else k):
                    (v.decode() if isinstance(v, bytes) else v)
                    for k, v in roast_meta_raw.items()
                }

            current_anchor = int(roast_meta.get(META_ANCHOR, 0))
            roast_turn_count = int(roast_meta.get(META_TURN_COUNT, 0))

            if roast_turn_count <= ROAST_RAW_TURN_COUNT:
                return

            # Set anchor so new turns during compression stay raw
            if self._redis:
                await self._redis.hset(
                    RedisKeys.roast_meta(self._user_id),
                    mapping={
                        META_ANCHOR: roast_turn_count,
                        META_ANCHOR_IN_PROGRESS: "1",
                    },
                )

            # Read all roast turns with turn numbers
            roast_turns_raw = await self._redis.lrange(
                RedisKeys.roast_turns(self._user_id), 0, -1
            ) if self._redis else []

            all_turns: list[tuple[int, Message]] = []
            for t in roast_turns_raw:
                d = json.loads(t.decode() if isinstance(t, bytes) else t)
                turn_num = d.pop("turn", 0)
                all_turns.append((turn_num, Message.from_dict(d)))

            # Compress turns between current_anchor and new_anchor, keeping ROAST_RAW_TURN_COUNT raw
            compressible = [
                (num, msg) for num, msg in all_turns
                if num > current_anchor and num <= roast_turn_count
            ]
            # Keep the most recent ROAST_RAW_TURN_COUNT raw
            if len(compressible) > ROAST_RAW_TURN_COUNT:
                to_compress = [msg for _, msg in compressible[:-ROAST_RAW_TURN_COUNT]]
            else:
                to_compress = []

            if not to_compress:
                if self._redis:
                    await self._redis.hset(
                        RedisKeys.roast_meta(self._user_id),
                        META_ANCHOR_IN_PROGRESS, "0",
                    )
                return

            existing_summary = ""
            roast_prompt = ""
            if self._redis:
                summary_raw = await self._redis.get(RedisKeys.roast_summary(self._user_id))
                if summary_raw:
                    existing_summary = summary_raw.decode() if isinstance(summary_raw, bytes) else summary_raw
                prompt_raw = await self._redis.get(RedisKeys.roast_prompt(self._user_id))
                if prompt_raw:
                    roast_prompt = prompt_raw.decode() if isinstance(prompt_raw, bytes) else prompt_raw

            new_summary = await self._compressor.compress_roast(
                to_compress,
                existing_summary=existing_summary,
                roast_prompt=roast_prompt,
            )

            if self._redis and new_summary:
                await self._redis.set(
                    RedisKeys.roast_summary(self._user_id), new_summary
                )
                await self._redis.hset(
                    RedisKeys.roast_meta(self._user_id),
                    mapping={
                        META_ANCHOR_IN_PROGRESS: "0",
                        META_LAST_COMPRESSED: time.time(),
                    },
                )

            logger.info(
                f"[Context] L4 roast compressed for user={self._user_id}: "
                f"{len(to_compress)} turns → {len(new_summary)} chars"
            )

        except Exception as e:
            logger.error(f"[Context] L4 roast compression failed: {e}")
            if self._redis:
                try:
                    await self._redis.hset(
                        RedisKeys.roast_meta(self._user_id),
                        META_ANCHOR_IN_PROGRESS, "0",
                    )
                except Exception:
                    pass
        finally:
            self._compression_in_flight = False

    # ── Internal: Read ────────────────────────────────────────────────

    async def _get_hot_turns(self, n: int, *, after_anchor: int = 0) -> list[Message]:
        """Read the last N raw turns from Redis.

        after_anchor: if > 0, only return turns with turn_number > after_anchor.
                      This ensures turns already covered by a compression summary
                      are not duplicated as raw turns.

        Falls back to returning the most recent N turns if no anchor is set.
        """
        if not self._redis:
            return []
        try:
            # Read more than N to account for anchor filtering
            read_count = max(n * 3, 30)
            raw = await self._redis.lrange(
                RedisKeys.turns(self._user_id), 0, read_count - 1
            )
            if not raw:
                return []

            turns: list[Message] = []
            for t in raw:
                d = json.loads(t.decode() if isinstance(t, bytes) else t)
                turn_num = d.pop("turn", 0)
                if after_anchor == 0 or turn_num > after_anchor:
                    turns.append(Message.from_dict(d))
                if len(turns) >= n:
                    break

            return turns
        except Exception as e:
            logger.warning(f"Redis LRANGE failed: {e}")
            return []

    async def _get_all_turns_with_numbers(self) -> list[tuple[int, Message, str | None]]:
        """Read all turns with turn numbers and roast_id."""
        if not self._redis:
            return []
        try:
            meta = await self._read_meta()
            roast_id = meta.get(META_ROAST_ID, "") or None
            raw = await self._redis.lrange(RedisKeys.turns(self._user_id), 0, -1)
            results = []
            for t in raw:
                d = json.loads(t.decode() if isinstance(t, bytes) else t)
                turn_num = d.pop("turn", 0)
                results.append((turn_num, Message.from_dict(d), roast_id))
            return results
        except Exception:
            return []

    async def _recover_turn_counter(self) -> int:
        """Recover turn_count from PG when Redis is cold or lost.

        Only called when Redis turn_count is 0. Reads MAX(turn_number)
        from PG — PG is the durable source of truth.
        """
        if not self._pg:
            return 0
        try:
            async with self._pg.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(turn_number), 0) FROM conversation_turns "
                    "WHERE user_id = $1",
                    self._user_id,
                )
                if row:
                    max_turn = row[0]
                    logger.info(
                        f"[Context] Recovered turn_count={max_turn} from PG "
                        f"for user={self._user_id}"
                    )
                    return max_turn
        except Exception as e:
            logger.warning(f"Failed to recover turn_counter from PG: {e}")
        return 0

    async def _read_meta(self) -> dict:
        if not self._redis:
            return {}
        try:
            raw = await self._redis.hgetall(RedisKeys.meta(self._user_id))
            if raw:
                return {
                    (k.decode() if isinstance(k, bytes) else k):
                    (int(v) if (v.decode() if isinstance(v, bytes) else v).isdigit()
                     else v.decode() if isinstance(v, bytes) else v)
                    for k, v in raw.items()
                }
        except Exception:
            pass
        return {}

    async def _read_summary(self, tier: str) -> str:
        if not self._redis:
            return ""
        try:
            key = (
                RedisKeys.summary_recent(self._user_id) if tier == "recent"
                else RedisKeys.summary_global(self._user_id)
            )
            raw = await self._redis.get(key)
            if raw:
                return raw.decode() if isinstance(raw, bytes) else raw
        except Exception:
            pass
        return ""

    async def _read_game_state(self) -> dict:
        if not self._redis:
            return {}
        try:
            raw = await self._redis.hgetall(RedisKeys.game_state(self._user_id))
            if raw:
                return {
                    (k.decode() if isinstance(k, bytes) else k):
                    (v.decode() if isinstance(v, bytes) else v)
                    for k, v in raw.items()
                }
        except Exception:
            pass
        return {}

    async def _load_user_memory(self) -> None:
        if not self._redis:
            return
        try:
            raw = await self._redis.hgetall(RedisKeys.user_memory(self._user_id))
            if raw:
                h = {
                    (k.decode() if isinstance(k, bytes) else k):
                    (v.decode() if isinstance(v, bytes) else v)
                    for k, v in raw.items()
                }
                self._user_memory = UserMemory.from_hash(h)
        except Exception:
            pass

    # ── Internal: Write ───────────────────────────────────────────────

    async def _flush_pg_buffer(self) -> None:
        if not self._pg:
            return
        async with self._pg_lock:
            if not self._pg_buffer:
                return
            batch = self._pg_buffer[:]
            self._pg_buffer.clear()

        try:
            async with self._pg.acquire() as conn:
                async with conn.transaction():
                    for turn_number, turn, roast_id in batch:
                        await conn.execute(
                            """INSERT INTO conversation_turns
                               (user_id, turn_number, role, content,
                                tool_calls, tool_call_id, name, partial,
                                roast_id)
                               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                               ON CONFLICT (user_id, turn_number) DO NOTHING""",
                            self._user_id, turn_number,
                            turn.role, turn.content,
                            _serialize_tool_calls(turn.tool_calls),
                            turn.tool_call_id,
                            turn.name,
                            turn.partial,
                            roast_id,
                        )
            logger.debug(f"Flushed {len(batch)} turns to PG")
        except Exception as e:
            logger.warning(f"PG flush failed: {e}")
            async with self._pg_lock:
                self._pg_buffer = batch + self._pg_buffer

    async def _persist_turns(self, turns: list[tuple[int, Message, str | None]]) -> None:
        if not self._pg:
            return
        try:
            async with self._pg.acquire() as conn:
                async with conn.transaction():
                    for turn_number, turn, roast_id in turns:
                        await conn.execute(
                            """INSERT INTO conversation_turns
                               (user_id, turn_number, role, content,
                                tool_calls, tool_call_id, name, partial,
                                roast_id)
                               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                               ON CONFLICT (user_id, turn_number) DO NOTHING""",
                            self._user_id, turn_number,
                            turn.role, turn.content,
                            _serialize_tool_calls(turn.tool_calls),
                            turn.tool_call_id,
                            turn.name,
                            turn.partial,
                            roast_id,
                        )
            logger.info(f"Persisted {len(turns)} turns to PG")
        except Exception as e:
            logger.error(f"PG persist failed: {e}")

    # ── Internal: L3 Compression ──────────────────────────────────────

    def _schedule_compression(self) -> None:
        """Fire-and-forget L3 session compression (free chat only)."""
        self._compression_in_flight = True
        asyncio.create_task(self._do_compress())

    async def _do_compress(self) -> None:
        """Incremental L3 compression with anchor.

        Only compresses new turns since the last anchor. Sets a new anchor
        so incoming turns during compression remain raw.
        """
        try:
            meta = await self._read_meta()
            current_tier = int(meta.get(META_TIER, 0))
            current_anchor = int(meta.get(META_ANCHOR, 0))
            turn_count = int(meta.get(META_TURN_COUNT, 0))

            # Set anchor before compression
            new_anchor = turn_count
            if self._redis:
                await self._redis.hset(
                    RedisKeys.meta(self._user_id),
                    mapping={
                        META_ANCHOR: new_anchor,
                        META_ANCHOR_IN_PROGRESS: "1",
                    },
                )

            # Get turns between the old and new anchor
            all_turns = await self._get_all_turns_with_numbers()
            if not all_turns:
                return

            # Sort by turn_number (Redis LPUSH means newest first; we want ascending)
            all_turns.sort(key=lambda x: x[0])

            # Compressible: turns after old anchor, up to new anchor, excluding last RAW_TURN_COUNT
            compressible = [
                (num, msg) for num, msg, _ in all_turns
                if num > current_anchor and num <= new_anchor
            ]
            if len(compressible) <= RAW_TURN_COUNT:
                if self._redis:
                    await self._redis.hset(
                        RedisKeys.meta(self._user_id),
                        META_ANCHOR_IN_PROGRESS, "0",
                    )
                return

            to_compress = [msg for _, msg in compressible[:-RAW_TURN_COUNT]]

            if current_tier == 0:
                summary = await self._compressor.compress_tier_1(to_compress)
                if self._redis and summary:
                    await self._redis.set(
                        RedisKeys.summary_recent(self._user_id), summary
                    )
                    await self._redis.hset(
                        RedisKeys.meta(self._user_id),
                        mapping={
                            META_TIER: 1,
                            META_LAST_COMPRESSED: time.time(),
                            META_ANCHOR_IN_PROGRESS: "0",
                        },
                    )
            else:
                existing = (
                    await self._read_summary("global") if current_tier >= 2
                    else await self._read_summary("recent")
                )
                new_summary = await self._compressor.compress_tier_2(
                    existing, to_compress
                )
                if self._redis and new_summary:
                    key = (
                        RedisKeys.summary_global(self._user_id)
                        if current_tier >= 2
                        else RedisKeys.summary_global(self._user_id)
                    )
                    await self._redis.set(key, new_summary)
                    await self._redis.hset(
                        RedisKeys.meta(self._user_id),
                        mapping={
                            META_TIER: 2,
                            META_LAST_COMPRESSED: time.time(),
                            META_ANCHOR_IN_PROGRESS: "0",
                        },
                    )

            # L2: Extract user facts alongside compression
            await self._extract_and_merge_facts(to_compress)

            logger.info(
                f"[Context] L3 compressed for user={self._user_id}: "
                f"tier {current_tier} → {current_tier + 1}, "
                f"{len(to_compress)} turns, anchor={current_anchor}→{new_anchor}"
            )

        except Exception as e:
            logger.error(f"[Context] L3 compression failed: {e}")
            if self._redis:
                try:
                    await self._redis.hset(
                        RedisKeys.meta(self._user_id),
                        META_ANCHOR_IN_PROGRESS, "0",
                    )
                except Exception:
                    pass
        finally:
            self._compression_in_flight = False

    async def _extract_and_merge_facts(self, turns: list[Message]) -> None:
        """L2: Extract facts (layer 1) → persist to PG → rebuild profile (layer 2)."""
        try:
            fact_dicts = await self._compressor.extract_facts(turns)
            if not fact_dicts:
                return

            # Layer 1: Persist extracted facts to PG (dedup by UNIQUE constraint)
            if self._pg:
                await self._persist_facts(fact_dicts)

            # Layer 2: Rebuild profile_summary from all facts
            if self._pg or self._redis:
                await self._rebuild_profile()

        except Exception as e:
            logger.warning(f"[Context] L2 fact extraction failed: {e}")

    async def _persist_facts(self, fact_dicts: list[dict]) -> None:
        """Write extracted facts to PG user_facts (ON CONFLICT DO NOTHING)."""
        if not self._pg:
            return
        try:
            async with self._pg.acquire() as conn:
                async with conn.transaction():
                    for fd in fact_dicts:
                        await conn.execute(
                            """INSERT INTO user_facts (user_id, fact, category)
                               VALUES ($1, $2, $3)
                               ON CONFLICT (user_id, fact) DO NOTHING""",
                            self._user_id,
                            fd.get("fact", ""),
                            fd.get("category", "personal"),
                        )
        except Exception as e:
            logger.warning(f"Failed to persist facts: {e}")

    async def _rebuild_profile(self) -> None:
        """Incremental: merge new facts into existing profile_summary.

        Only fetches facts added since the last profile update, then merges
        into the existing profile. Avoids unbounded fact accumulation in the LLM call.
        """
        existing_profile = self.user_memory.profile_summary

        # Read existing profile from PG if Redis is empty
        if not existing_profile and self._pg:
            try:
                async with self._pg.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT profile_summary, updated_at FROM user_memory WHERE user_id=$1",
                        self._user_id,
                    )
                    if row and row["profile_summary"]:
                        existing_profile = row["profile_summary"]
            except Exception:
                pass

        # Read only NEW facts (since last profile update) from PG
        new_facts: list[str] = []
        if self._pg:
            try:
                last_update = None
                if existing_profile:
                    try:
                        async with self._pg.acquire() as conn:
                            row = await conn.fetchrow(
                                "SELECT updated_at FROM user_memory WHERE user_id=$1",
                                self._user_id,
                            )
                            if row:
                                last_update = row["updated_at"]
                    except Exception:
                        pass

                async with self._pg.acquire() as conn:
                    if last_update:
                        rows = await conn.fetch(
                            "SELECT fact, category FROM user_facts WHERE user_id=$1 "
                            "AND created_at > $2 ORDER BY created_at DESC",
                            self._user_id, last_update,
                        )
                    else:
                        # First time: read all facts
                        rows = await conn.fetch(
                            "SELECT fact, category FROM user_facts WHERE user_id=$1 "
                            "ORDER BY created_at DESC",
                            self._user_id,
                        )
                    new_facts = [
                        f"{row['fact']} ({row['category']})"
                        for row in rows
                    ]
            except Exception:
                pass

        if not new_facts and existing_profile:
            return  # nothing new to merge

        if not new_facts:
            return

        # Incremental merge: existing profile + new facts
        profile = await self._compressor.summarize_profile(
            new_facts, existing=existing_profile
        )
        if not profile:
            return

        # Persist to Redis
        self._user_memory = UserMemory(
            user_id=self._user_id,
            profile_summary=profile,
            stats=self.user_memory.stats,
        )
        if self._redis:
            await self._redis.hset(
                RedisKeys.user_memory(self._user_id),
                mapping=self._user_memory.to_hash(),
            )

        # Persist to PG
        if self._pg:
            try:
                async with self._pg.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO user_memory (user_id, profile_summary, updated_at)
                           VALUES ($1, $2, NOW())
                           ON CONFLICT (user_id) DO UPDATE SET
                           profile_summary = EXCLUDED.profile_summary,
                           updated_at = NOW()""",
                        self._user_id, profile,
                    )
            except Exception as e:
                logger.warning(f"Failed to persist profile_summary: {e}")

    async def _read_cached_facts(self) -> list[str]:
        """Read cached facts from Redis user_memory hash (fallback)."""
        if not self._redis:
            return []
        try:
            raw = await self._redis.hget(
                RedisKeys.user_memory(self._user_id), "facts_text"
            )
            if raw:
                text = raw.decode() if isinstance(raw, bytes) else raw
                return [f.strip() for f in text.split("\n") if f.strip()]
        except Exception:
            pass
        return []

    # ── Segment ───────────────────────────────────────────────────────

    async def _get_closed_segment_summaries(self, limit: int = 5) -> list[str]:
        """Get summaries of recently closed segments, newest first."""
        if not self._redis:
            return []
        try:
            raw_list = await self._redis.lrange(
                RedisKeys.segments(self._user_id), 0, limit - 1
            )
            summaries = []
            for raw in raw_list:
                d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                seg = ContextSegment.from_dict(d)
                if seg.status == "closed" and seg.summary:
                    summaries.append(seg.summary)
            return summaries
        except Exception:
            return []

    # ── Timer ─────────────────────────────────────────────────────────

    def _start_flush_timer(self) -> None:
        if self._flush_started:
            return
        self._flush_started = True

        async def timer():
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            await self._flush_pg_buffer()
            self._flush_started = False

        asyncio.create_task(timer())
