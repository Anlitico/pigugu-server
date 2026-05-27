# pigagent/context/compression/compressor.py
"""ContextCompressor  -  unified compression pipeline.

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
        model: str = "qwen-plus",
    ) -> None:
        """Auto-detect scenario and dispatch."""
        snap = ContextSnapshot(records)
        if not await snap.should_compress(existing_summary=existing_summary, model=model):
            return

        from context.storage.redis import RedisStorage
        from context.storage.pg import PgStorage

        self._store = RedisStorage(user_id, self._store_client)
        self._pg_store = PgStorage(user_id, self._pg_pool)

        if snap.scenario == "roast":
            await self._run_roast(user_id, snap, existing_summary, model)
        else:
            await self._run_free_chat(user_id, snap, existing_summary, model)

    # ── Scenario: free_chat (L2 + L3) ─────────────────────────────────

    async def _run_free_chat(self, user_id: str, snap: ContextSnapshot,
                              existing_summary: str, model: str) -> None:
        """L2 + L3. All records go to L3."""
        await self._store.set_compressing(True)
        l3_msgs, _, end_turn, _ = await self._prepare_from(snap, model=model)
        if not l3_msgs:
            await self._store.set_compressing(False)
            return

        try:
            l2_facts, l3_text = await asyncio.gather(
                extract_facts(l3_msgs, model=model),
                self._compress_l3(existing_summary, l3_msgs, model),
                return_exceptions=True,
            )
            if isinstance(l2_facts, BaseException):
                l2_facts = []
            if isinstance(l3_text, BaseException):
                l3_text = existing_summary

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

    # ── Scenario: roast (L2 + L3 + L4) ────────────────────────────────

    async def _run_roast(self, user_id: str, snap: ContextSnapshot,
                          existing_summary: str, model: str) -> None:
        """L2 + L3 (pre-roast only) + L4 (roast conversation, prompt preserved verbatim)."""
        await self._store.set_compressing(True)
        l3_msgs, l4_msgs, end_turn, roast_prompt = await self._prepare_from(snap, model=model)

        try:
            tasks = []
            if l3_msgs:
                tasks.append(extract_facts(l3_msgs, model=model))
                tasks.append(self._compress_l3(existing_summary, l3_msgs, model))
            else:
                tasks.extend([asyncio.sleep(0), asyncio.sleep(0)])

            if l4_msgs:
                data = await self._store.read_summaries()
                existing_roast = data.get("l4_roast", "")
                tasks.append(compress_roast(l4_msgs, existing_summary=existing_roast,
                                            model=model))
            else:
                tasks.append(asyncio.sleep(0))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            l2_facts = results[0] if not isinstance(results[0], BaseException) else []
            l3_text = results[1] if not isinstance(results[1], BaseException) else existing_summary
            l4_text = results[2] if not isinstance(results[2], BaseException) else ""

            l2_profile = await self._write_l2(l2_facts)
            await self._persist_summaries(
                end_turn, l2_profile=l2_profile, l3_session=l3_text,
                l4_roast=l4_text, roast_id=snap.roast_instance_id,
                roast_prompt=roast_prompt, model=model,
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
        roast_id: str = "", roast_prompt: str = "", model: str = "",
    ) -> None:
        """Write all three layer summaries to Redis + PG in one call."""

        # Redis — single key
        await self._store.write_summaries(
            end_turn,
            l2_profile=l2_profile, l3_session=l3_session, l4_roast=l4_roast,
            roast_id=roast_id, roast_prompt=roast_prompt,
        )

        # PG — single row
        await self._pg_store.write_summary_row(
            end_turn,
            l2_profile=l2_profile, l3_session=l3_session, l4_roast=l4_roast,
            roast_id=roast_id, roast_prompt=roast_prompt, model_used=model,
        )

    # ── Roast Prompt Discovery ──────────────────────────────────────

    @staticmethod
    def _find_roast_prompt(records: list) -> tuple[str, str]:
        """Find the active roast prompt from raw conversation records.

        Uses the latest roast_instance_id (current roast) and returns
        the content of its first occurrence — the prompt turn, which
        should never be compressed.

        Returns (prompt_text, roast_instance_id). Both empty if no roast found.
        """
        if not records:
            return "", ""

        # Latest roast_instance_id = current active roast
        latest_rid = ""
        for r in reversed(records):
            if r.roast_instance_id:
                latest_rid = r.roast_instance_id
                break
        if not latest_rid:
            return "", ""

        # First occurrence of this id = the prompt turn
        for r in records:
            if r.roast_instance_id == latest_rid:
                return r.content, latest_rid

        return "", ""

    # ── Helpers ───────────────────────────────────────────────────────

    async def _prepare_from(
        self, snap: ContextSnapshot, *, model: str = "qwen-plus",
    ) -> tuple[list, list | None, int, str]:
        """Extract L3/L4 message groups from snapshot.

        free_chat: all records  ->  L3, no L4, no roast_prompt.
        roast: pre_roast  ->  L3; prompt turn preserved verbatim;
               remaining roast turns  ->  L4 if token threshold met.

        Returns (l3_msgs, l4_msgs | None, end_turn, roast_prompt).
        """
        if snap.scenario == "free_chat":
            l3 = snap.records
            return snap.to_messages(l3), None, l3[-1].turn_number, ""

        pre_roast, roast = snap.split()
        l3 = snap.to_messages(pre_roast) if pre_roast else []

        # Roast prompt: try raw records first, fall back to existing summaries
        roast_prompt_text, rid = self._find_roast_prompt(snap.records)
        if not roast_prompt_text:
            data = await self._store.read_summaries()
            if data.get("roast_id") == snap.roast_instance_id:
                roast_prompt_text = data.get("roast_prompt", "")

        # L4: compress roast turns after the prompt (exclude prompt itself)
        l4 = None
        l4_end = 0
        prompt_turn = roast[0].turn_number if roast else 0
        conversation_turns = [r for r in roast if r.turn_number > prompt_turn]
        if conversation_turns and await snap.should_compress_l4(model=model):
            l4 = snap.to_messages(conversation_turns)
            l4_end = conversation_turns[-1].turn_number

        l3_end = pre_roast[-1].turn_number if pre_roast else 0
        end = max(l3_end, l4_end, prompt_turn)

        return l3, l4, end, roast_prompt_text

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
