# pigagent/context/compression/compressor.py
"""ContextCompressor  -  unified compression pipeline.

Receives a standardized list of ConversationRecord from
WorkingContext.to_records(). Splits by layer boundaries (L2, L3,
roast_prompt_turn, L4) and compresses each independently.

Two scenarios (auto-detected via ContextSnapshot):
  free_chat   ->  L2 extract + L3 merge
  roast       ->  L2 extract + L3 merge + L4 roast compress

Phase 1  -  Concurrent LLM calls
Phase 2  -  Sequential writes: Redis first, then PG
"""

from __future__ import annotations

import asyncio

from loguru import logger

from context.snapshot import ContextSnapshot
from context.compression.l2_facts import extract_facts, summarize_profile
from context.compression.l3_session import compress_turns, merge_summary
from context.compression.l4_roast import compress_roast


def _strip_label(content: str) -> str:
    """Strip the [Label]\\n prefix from a summary virtual record."""
    if content and "\n" in content:
        return content.split("\n", 1)[1]
    return content


class ContextCompressor:
    """Unified compression pipeline. One entry point: run()."""

    def __init__(self, *, redis_client=None, pg_pool=None):
        self._store_client = redis_client
        self._pg_pool = pg_pool

    # ── Entry ─────────────────────────────────────────────────────────

    async def run(
        self, *,
        user_id: str,
        records: list,
        existing_summary: str = "",
        model: str = "qwen-plus-us",
    ) -> None:
        """Auto-detect scenario and dispatch.

        records is a unified list from WorkingContext.to_records():
        virtual turns (L2=-3, L3=-2, L4=-1) + real turns (>0).
        """
        # Only real turns for compression trigger checks
        real_turns = [r for r in records if r.turn_number > 0]
        snap = ContextSnapshot(real_turns)
        if not await snap.should_compress(existing_summary=existing_summary, model=model):
            return

        from context.storage.redis import RedisStorage
        from context.storage.pg import PgStorage

        self._store = RedisStorage(user_id, self._store_client)
        self._pg_store = PgStorage(user_id, self._pg_pool)

        # Pass unified list inside snap for splitting
        snap.records = records  # temporarily override for splitting

        if snap.scenario == "roast":
            await self._run_roast(user_id, snap, existing_summary, model)
        else:
            await self._run_free_chat(user_id, snap, existing_summary, model)

    # ── Scenario: free_chat (L2 + L3) ──────────────────────────────

    async def _run_free_chat(self, user_id: str, snap: ContextSnapshot,
                              existing_summary: str, model: str) -> None:
        """L2 + L3. All real turns go to L3, L2 from full context minus L2 itself."""
        await self._store.set_compressing(True)

        # Split: real turns (pos turn_number) vs virtual (summaries)
        real_turns = [r for r in snap.records if r.turn_number > 0]
        virtual = {r.turn_number: r for r in snap.records if r.turn_number <= 0}
        if not real_turns:
            await self._store.set_compressing(False)
            return

        l3_msgs = snap.to_messages(real_turns)
        existing_l3 = virtual.get(-2)
        existing_l3_text = _strip_label(existing_l3.content) if existing_l3 else existing_summary

        # L2: full context minus L2 itself
        l2_records = [r for r in snap.records if r.turn_number != -3]
        l2_msgs = snap.to_messages(l2_records)

        end_turn = real_turns[-1].turn_number

        try:
            l2_facts, l3_text = await asyncio.gather(
                extract_facts(l2_msgs, model=model),
                self._compress_l3(existing_l3_text, l3_msgs, model),
                return_exceptions=True,
            )
            if isinstance(l2_facts, BaseException):
                l2_facts = []
            if isinstance(l3_text, BaseException):
                l3_text = existing_l3_text

            l2_profile = await self._write_l2(l2_facts)
            await self._persist_summaries(end_turn, l2_profile=l2_profile, l3_session=l3_text, model=model)
            await self._store.set_compressing(False)
            logger.info(f"[Compress] free_chat u={user_id}: L2={len(l2_facts)}f L3={len(l3_text)}c")

        except Exception as e:
            logger.error(f"[Compress] free_chat failed: {e}")
            try:
                await self._store.set_compressing(False)
            except Exception:
                pass

    # ── Scenario: roast (L2 + L3 + L4) ──────────────────────────────

    async def _run_roast(self, user_id: str, snap: ContextSnapshot,
                          existing_summary: str, model: str) -> None:
        """L2 + L3 + L4. Split unified list by roast_prompt_turn anchor."""
        await self._store.set_compressing(True)

        real_turns = [r for r in snap.records if r.turn_number > 0]
        virtual = {r.turn_number: r for r in snap.records if r.turn_number <= 0}

        # Find latest active roast id, then its first occurrence (= prompt turn)
        latest_rid = ""
        for r in reversed(real_turns):
            if r.roast_instance_id:
                latest_rid = r.roast_instance_id
                break
        if not latest_rid:
            await self._store.set_compressing(False)
            return

        prompt_turn = 0
        prompt_text = ""
        prompt_rid = latest_rid
        for r in real_turns:
            if r.roast_instance_id == latest_rid:
                prompt_turn = r.turn_number
                prompt_text = r.content
                break

        # L3: real turns before prompt_turn (pre-roast, regardless of rid)
        l3_real = [r for r in real_turns if r.turn_number < prompt_turn]
        existing_l3 = virtual.get(-2)
        existing_l3_text = _strip_label(existing_l3.content) if existing_l3 else existing_summary

        # L4: real turns after prompt_turn with same rid
        l4_real = [r for r in real_turns if r.turn_number > prompt_turn and r.roast_instance_id == prompt_rid]
        existing_l4 = virtual.get(-1)
        existing_l4_text = _strip_label(existing_l4.content) if existing_l4 else ""

        # L2: full context minus L2 itself
        l2_records = [r for r in snap.records if r.turn_number != -3]

        try:
            tasks = []
            if l3_real:
                l3_msgs = snap.to_messages(l3_real)
                tasks.append(extract_facts(snap.to_messages(l2_records), model=model))
                tasks.append(self._compress_l3(existing_l3_text, l3_msgs, model))
            else:
                tasks.extend([asyncio.sleep(0), asyncio.sleep(0)])

            if l4_real and await snap.should_compress_l4(model=model):
                l4_msgs = snap.to_messages(l4_real)
                tasks.append(compress_roast(l4_msgs, existing_summary=existing_l4_text,
                                            model=model))
            else:
                tasks.append(asyncio.sleep(0))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            l2_facts = results[0] if not isinstance(results[0], BaseException) else []
            l3_text = results[1] if not isinstance(results[1], BaseException) else existing_l3_text
            l4_text = results[2] if not isinstance(results[2], BaseException) else ""

            l2_profile = await self._write_l2(l2_facts)
            await self._persist_summaries(
                max(real_turns[-1].turn_number, prompt_turn),
                l2_profile=l2_profile, l3_session=l3_text,
                l4_roast=l4_text, roast_id=prompt_rid,
                roast_prompt=prompt_text, roast_prompt_turn=prompt_turn,
                model=model,
            )

            await self._store.set_compressing(False)
            logger.info(f"[Compress] roast u={user_id}: L2={len(l2_facts)}f L3={len(l3_text)}c L4={'Y' if l4_text else 'N'}")

        except Exception as e:
            logger.error(f"[Compress] roast failed: {e}")
            try:
                await self._store.set_compressing(False)
            except Exception:
                pass

    # ── Unified Persistence ─────────────────────────────────────────

    async def _persist_summaries(
        self, end_turn: int, *,
        l2_profile: str = "", l3_session: str = "", l4_roast: str = "",
        roast_id: str = "", roast_prompt: str = "", roast_prompt_turn: int = 0,
        model: str = "",
    ) -> None:
        """Write all three layer summaries to Redis + PG in one call."""

        await self._store.write_summaries(
            end_turn,
            l2_profile=l2_profile, l3_session=l3_session, l4_roast=l4_roast,
            roast_id=roast_id, roast_prompt=roast_prompt,
            roast_prompt_turn=roast_prompt_turn,
        )

        await self._pg_store.write_summary_row(
            end_turn,
            l2_profile=l2_profile, l3_session=l3_session, l4_roast=l4_roast,
            roast_id=roast_id, roast_prompt=roast_prompt,
            roast_prompt_turn=roast_prompt_turn, model_used=model,
        )

    # ── Helpers ─────────────────────────────────────────────────────

    async def _compress_l3(self, existing_summary: str, msgs: list, model: str) -> str:
        if existing_summary:
            return await merge_summary(existing_summary, msgs, model=model)
        return await compress_turns(msgs, model=model)

    async def _write_l2(self, facts: list[dict]) -> str:
        if not facts:
            return ""
        await self._pg_store.persist_facts(facts)
        return await self._rebuild_profile()

    # ── Profile ───────────────────────────────────────────────────────

    async def _rebuild_profile(self) -> str:
        existing, _ = await self._pg_store.read_profile()
        new_facts = await self._pg_store.read_new_facts()
        if not new_facts:
            return existing

        profile = await summarize_profile(new_facts, existing=existing)
        if not profile:
            return existing

        await self._pg_store.upsert_profile(profile)
        return profile
