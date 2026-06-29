"""GameMode ABC and Trigger  -  abstract interface for roast game modes."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, ClassVar, TYPE_CHECKING

from loguru import logger
from roast.constants import ROAST_BODY_PREFIX
from roast.types import Mode, Phase
from roast import pending

if TYPE_CHECKING:
    from roast.state import RoastState
    from prompts import PromptStore


@dataclass(frozen=True)
class Trigger:
    """A trigger condition + its prompt template.

    When check returns True, the prompt is injected into the next turn.
    Evaluated in order; first match wins.

    prompt can be a static str or a Callable[[RoastState, PromptStore], str]
    for dynamic templates (e.g. referencing state.extra fields).

    If affects_phase is True, the state phase is updated to CLOSING
    when this trigger fires. Used for game-ending triggers.
    """

    name: str
    check: Callable[[RoastState, list], bool] = field(repr=False)
    prompt: str | Callable[..., str] = field(repr=False)
    affects_phase: bool = False


class GameMode(ABC):
    """Abstract base for a roast game mode.

    Subclasses define their own rules, triggers, scoring logic,
    and a director prompt. The director LLM runs every tick to
    evaluate the conversation and inject guidance to the actor.
    """

    mode: Mode

    @abstractmethod
    async def get_system_prompt_extension(self, prompt_store: PromptStore | None) -> str:
        """Return the game mode system prompt extension.

        *prompt_store* is a :class:`~prompts.PromptStore` instance.
        """
        ...

    @abstractmethod
    async def get_director_prompt(self, prompt_store: PromptStore | None) -> str:
        """System prompt for the director LLM. Each mode writes its own.

        *prompt_store* is a :class:`~prompts.PromptStore` instance.
        """
        ...

    @abstractmethod
    def get_director_schema(self) -> dict:
        """JSON Schema for the director LLM output. Each mode defines its own.

        Returns a dict suitable for use as the ``json_schema`` in
        ``response_format``.  Must include ``name``, ``strict``, and
        ``schema`` keys.
        """
        ...

    @abstractmethod
    def score(self, state: RoastState) -> dict:
        """Compute the final score summary for this game mode.

        Called at settlement time. Returns a dict with mode-specific
        scoring data (total_score, avg_score, best_rating, etc. for Hot
        Take; final_user_support, result, etc. for Debate).
        """
        ...

    @staticmethod
    def init_extra() -> dict:
        """Optional initial extra state. Override to set mode-specific defaults."""
        return {"best_take": ""}

    max_turns: int

    # ── Director ────────────────────────────────────────────────────────────

    async def _direct(
        self, state: RoastState, *, wc, current_msg=None,
        prompt_store: PromptStore | None = None,
    ) -> dict:
        """Run the director LLM to evaluate the conversation.

        Builds the message list from WorkingContext.raw_records (which carry
        proper roast_instance_id) instead of receiving an opaque records list.

        Returns: {"action": "none"|"inject", "best_take": str|null,
                  "prompt": str|null, "close": bool}
        On failure, returns action="none" gracefully.
        """
        # Degraded: no WorkingContext available (shouldn't happen in normal
        # operation — callers guard this — but defend against it anyway).
        if wc is None:
            return {"action": "none", "best_take": None, "prompt": None, "close": False}

        from agent_config import get_config
        from core.llm import get_llm
        from core.llm.types import Message as LLMMessage

        director_model = get_config().DIRECTOR_MODEL

        # Build director messages: system prompt + current roast conversation only
        if prompt_store is None:
            # Fallback: no PromptStore available — skip director (degraded mode).
            # In production, create_pig_agent() always provides a PromptStore.
            logger.warning(
                f"[{self.mode}] No PromptStore — director degraded"
            )
            return {"action": "none", "best_take": None, "prompt": None, "close": False}

        director_prompt = await self.get_director_prompt(prompt_store)
        messages = [LLMMessage.system(director_prompt)]
        roast_id = state.roast_instance_id
        has_roast_body = False

        for r in wc.raw_records:
            role = r.role
            content = r.content
            if role not in ("system", "user", "assistant", "tool") or not content:
                continue
            rid = r.roast_instance_id
            is_roast_body = (
                role == "system"
                and ROAST_BODY_PREFIX in content
                and not has_roast_body
            )
            if rid is not None and rid != roast_id and not is_roast_body:
                continue
            if is_roast_body:
                has_roast_body = True
            messages.append(LLMMessage(role=role, content=content,))

        # Append the current user message that triggered this round.
        # It may not be in wc.raw_records yet (snapshot taken before
        # add_turn for this message completed).
        if current_msg is not None:
            messages.append(LLMMessage(
                role=getattr(current_msg, "role", "user"),
                content=getattr(current_msg, "content", ""),
            ))

        try:
            llm = get_llm(director_model)
            response = await llm.chat(
                messages,
                model=director_model,
                response_format={
                    "type": "json_schema",
                    "json_schema": self.get_director_schema(),
                },
            )

            from utils import safe_parse_llm_json
            result = safe_parse_llm_json(response.content)
            logger.info(
                f"[{self.mode}] Director: action={result.get('action')} "
                f"best_take={'yes' if result.get('best_take') else 'no'} "
                f"close={result.get('close', False)}"
            )
            # Use the global turn_number from the last assistant record in
            # WorkingContext.raw_records. This aligns director log entries
            # with agent_conversations.turn_number so the entire data flow
            # can be joined on a single turn_number.
            # We use assistant (not user) because STT may split user input
            # into multiple fragments — assistant responses are always whole.
            turn_number = 0
            for r in reversed(wc.raw_records):
                if getattr(r, "role", "") == "assistant":
                    turn_number = r.turn_number
                    break
            # Fallback: if no assistant record exists yet (extremely rare —
            # only before the first assistant response), use state.turn_count
            # to avoid (roast_instance_id, 0) collision.
            if turn_number == 0:
                turn_number = state.turn_count
            asyncio.create_task(_write_director_log(
                state.roast_instance_id, turn_number, result,
            ))
            return result
        except Exception as e:
            logger.error(f"[{self.mode}] Director failed: {e}")
            return {"action": "none", "best_take": None, "prompt": None, "close": False}

    # ── Director result hook ─────────────────────────────────────────────

    async def _on_director_result(
        self, state: RoastState, director_result: dict, redis,
    ) -> None:
        """Hook called after director evaluation completes.

        Override in subclasses to publish real-time scoring events
        (e.g. ``roast_score``, ``debate_judge``) to the App via Redis.
        """
        pass

    @property
    def triggers(self) -> list[Trigger]:
        """Ordered list of trigger conditions. First match wins.

        Override to add mode-specific triggers. Default checks max_turns.
        """
        return [
            Trigger(
                name="ending_max_turns",
                check=lambda s, r: s.turn_count >= self.max_turns,
                prompt=(
                    "THE GAME IS OVER. Wrap up now with a closing thought. "
                    "Stay in character."
                ),
            )
        ]

    # ── Advance ─────────────────────────────────────────────────────────

    async def tick(
        self,
        state: RoastState,
        *,
        wc,  # WorkingContext — required, carries raw_records with global turn_number
        redis,
        pg_pool=None,
        current_msg=None,  # current user Message (may not be in wc snapshot yet)
        prompt_store: PromptStore | None = None,
    ) -> str | None:
        """Advance state after one user turn.

        1. Bump turn count
        2. Run director LLM → update best_take, inject prompt if needed
        3. Check code triggers (safety net)
        4. Persist state

        Returns the prompt string if a trigger or director fired, else None.
        """
        if state.phase != Phase.ACTIVE:
            return None

        state.turn_count += 1

        # ── Director (async, fire-and-forget style but awaited) ──────────
        director_prompt: str | None = None
        try:
            director_result = await self._direct(
                state, wc=wc, current_msg=current_msg, prompt_store=prompt_store,
            )

            if director_result.get("best_take"):
                state.extra["best_take"] = director_result["best_take"]

            # Inject: Director wants to give the Agent a hint (→ [Game Event] in
            # next turn's messages, NOT persisted to agent_conversations).
            if director_result.get("action") == "inject" and director_result.get("prompt"):
                director_prompt = director_result["prompt"]

            # Close: Director signals the game should end (→ phase=CLOSING
            # persisted in RoastState, affects all future turns).
            # Push real-time scoring event to App via Redis (mode-specific).
            # Must run BEFORE the close check so the hook (e.g. debate KO)
            # can set CLOSING phase and write its own pending prompt first.
            hook_closed = False
            try:
                await self._on_director_result(state, director_result, redis)
                hook_closed = (state.phase == Phase.CLOSING)
            except Exception as e:
                logger.error(f"[{self.mode}] _on_director_result failed: {e}")

            if director_result.get("close"):
                state.phase = Phase.CLOSING
                # Only set generic closing prompt if the hook didn't already
                # trigger a mode-specific close (e.g. debate KO verdict).
                if not director_prompt and not hook_closed:
                    director_prompt = (
                        "THE GAME IS OVER. Wrap up now with a closing thought. "
                        "Stay in character. Call mark_roast_complete when done."
                    )
                logger.info(
                    f"[{self.mode}] Director triggered CLOSING "
                    f"roast={state.roast_instance_id} turn={state.turn_count}"
                )

            if director_prompt:
                await pending.write(state.roast_instance_id, director_prompt, redis)
                await state.save(redis, pg_pool)
                return director_prompt
        except Exception as e:
            logger.error(f"[{self.mode}] Director error (degraded): {e}")

        # ── Code triggers (safety net) ──────────────────────────────────
        for trigger in self.triggers:
            try:
                if trigger.check(state, wc.raw_records):
                    if callable(trigger.prompt):
                        prompt = trigger.prompt(state, prompt_store)
                        if asyncio.iscoroutine(prompt):
                            prompt = await prompt
                    else:
                        prompt = trigger.prompt
                    await self._emit(state, trigger, prompt, redis)
                    await state.save(redis, pg_pool)
                    return prompt
            except Exception as e:
                logger.error(f"[{self.mode}] Trigger '{trigger.name}' failed: {e}")

        await state.save(redis, pg_pool)
        return None

    async def _emit(self, state: RoastState, trigger: Trigger, prompt: str, redis) -> None:
        """Write trigger prompt and update state."""
        await pending.write(state.roast_instance_id, prompt, redis)

        if trigger.affects_phase:
            state.phase = Phase.CLOSING

        logger.info(
            f"[{self.mode}] Triggered: {trigger.name} "
            f"roast={state.roast_instance_id} turn={state.turn_count}"
        )


async def _write_director_log(
    roast_instance_id: str, turn_number: int, result: dict,
) -> None:
    """Persist Director LLM decision to roast_director_logs for analysis.

    When multiple director evaluations happen for the same turn_number
    (e.g. STT splits one user utterance into fragments, each triggers its
    own LLM → director cycle), only the LAST one is kept via UPSERT.

    The full LLM output is stored as JSONB in ``raw_result`` so that
    mode-specific fields (score, rating, user_support, etc.) are preserved
    without requiring schema changes.
    """
    try:
        import json as _json
        from bootstrap.factory import get_pg_pool
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roast_director_logs
                   (roast_instance_id, turn_number, action, best_take, prompt, close, raw_result)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT (roast_instance_id, turn_number) DO UPDATE SET
                       action = EXCLUDED.action,
                       best_take = EXCLUDED.best_take,
                       prompt = EXCLUDED.prompt,
                       close = EXCLUDED.close,
                       raw_result = EXCLUDED.raw_result""",
                roast_instance_id,
                turn_number,
                result.get("action"),
                result.get("best_take"),
                result.get("prompt"),
                result.get("close", False),
                _json.dumps(result),
            )
    except Exception as e:
        logger.warning(f"[Director] Failed to write director log: {e}")

