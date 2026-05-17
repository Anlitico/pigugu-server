# agent/context/compression/compressor.py
"""ContextCompressor — unified compression pipeline.

Phase 1 — Concurrent: L2 extract + L3 merge + L4 roast
Phase 2 — Sequential writes: facts→PG, L3 summary→Redis, L4 roast→Redis
"""

from __future__ import annotations

import asyncio

from loguru import logger

from config import get_config

_cfg = get_config()

from context.schema import SummaryRecord
from context.compression.l2_facts import extract_facts, summarize_profile
from context.compression.l3_session import compress_tier_1, compress_tier_2
from context.compression.l4_roast import compress_roast


class ContextCompressor:
    """Unified compression pipeline. One entry point: run()."""

    def __init__(self, *, redis_store, pg_store):
        self._redis = redis_store
        self._pg = pg_store

    # ── Trigger logic ────────────────────────────────────────────────

    @staticmethod
    async def should_compress(records: list, *, model: str = "qwen3.6-plus") -> bool:
        """Token-budget-based: compress if raw turns exceed the threshold."""
        from core.llm import get_llm
        provider = get_llm(model)
        total_tokens = await provider.count_tokens([r.to_message() for r in records])
        return total_tokens > _cfg.CONTEXT_TOKEN_BUDGET_CAP

    @staticmethod
    def detect_scenario(records: list) -> str:
        """Detect scenario from records. "roast" if any record has roast_id, else "free_chat"."""
        for r in records:
            if r.roast_id:
                return "roast"
        return "free_chat"

    # ── Pipeline ─────────────────────────────────────────────────────

    async def run(
        self, *,
        user_id: str,
        records: list,          # list[ConversationRecord] from caller
        existing_summary: str = "",
        model: str = "qwen3.6-plus",
    ) -> None:
        """Pipeline. Records + existing_summary from caller. Auto-detects scenario."""
        if not await self.should_compress(records, model=model):
            return

        scenario = self.detect_scenario(records)
        await self._redis.set_compressing(True)

        try:
            sorted_records = sorted(records, key=lambda r: r.turn_number)
            keep_raw = _cfg.CONTEXT_RAW_TURN_COUNT
            to_compress = sorted_records[:-keep_raw]
            end_turn = to_compress[-1].turn_number
            compress_msgs = [r.to_message() for r in to_compress]

            tasks = [extract_facts(compress_msgs, model=model)]
            tasks.append(
                compress_tier_2(existing_summary, compress_msgs, model=model)
                if existing_summary
                else compress_tier_1(compress_msgs, model=model)
            )

            l4_future = None
            if scenario == "roast":
                existing_roast = await self._redis.read_roast_summary()
                roast_prompt = await self._redis.read_roast_prompt()
                roast_records = [r for r in to_compress if r.roast_id]
                if roast_records:
                    l4_future = asyncio.ensure_future(
                        compress_roast(
                            [r.to_message() for r in roast_records],
                            existing_summary=existing_roast,
                            roast_prompt=roast_prompt,
                            model=model,
                        )
                    )

            results = await asyncio.gather(*tasks, return_exceptions=True)
            l2_facts = results[0] if not isinstance(results[0], Exception) else []
            l3_text = results[1] if not isinstance(results[1], Exception) else existing_summary

            l4_text = None
            if l4_future:
                try:
                    l4_text = await l4_future
                except Exception:
                    pass

            # ── Phase 2: Sequential writes ──
            # Phase 2: Sequential writes — Redis first (hot path), then PG (durable)
            if l2_facts:
                await self._pg.persist_facts(l2_facts)
                await self._rebuild_profile()

            if l3_text:
                await self._redis.write_summary(SummaryRecord(
                    text=l3_text, end_turn=end_turn, tier=1,
                ))

            if l4_text:
                await self._redis.write_roast_summary(l4_text)

            await self._redis.set_compressing(False)

            logger.info(
                f"[Compress] user={user_id} {scenario}: {len(compress_msgs)}t → "
                f"L3={len(l3_text)}c, L2={len(l2_facts)}f, "
                f"L4={'Y' if l4_text else 'N'}, anchor={end_turn}"
            )

        except Exception as e:
            logger.error(f"[Compress] pipeline failed: {e}")
            try:
                await self._redis.set_compressing(False)
            except Exception:
                pass

    async def _rebuild_profile(self) -> None:
        """Rebuild L2 profile: PG facts → LLM summarize → Redis + PG user_memory."""
        from context.schema import UserMemory

        existing, _ = await self._pg.read_profile()
        new_facts = await self._pg.read_new_facts()
        if not new_facts and existing:
            return
        if not new_facts:
            return

        profile = await summarize_profile(new_facts, existing=existing)
        if not profile:
            return

        um = UserMemory(user_id="", profile_summary=profile)
        await self._redis.write_user_memory(um)
        await self._pg.upsert_profile(profile)
