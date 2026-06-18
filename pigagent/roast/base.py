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


@dataclass(frozen=True)
class Trigger:
    """A trigger condition + its prompt template.

    When check returns True, the prompt is injected into the next turn.
    Evaluated in order; first match wins.

    prompt can be a static str or a Callable[[RoastState], str] for
    dynamic templates (e.g. referencing state.extra fields).

    If affects_phase is True, the state phase is updated to CLOSING
    when this trigger fires. Used for game-ending triggers.
    """

    name: str
    check: Callable[[RoastState, list], bool] = field(repr=False)
    prompt: str | Callable[[RoastState], str] = field(repr=False)
    affects_phase: bool = False


class GameMode(ABC):
    """Abstract base for a roast game mode.

    Subclasses define their own rules, triggers, scoring logic,
    and a director prompt. The director LLM runs every tick to
    evaluate the conversation and inject guidance to the actor.
    """

    mode: Mode

    @property
    @abstractmethod
    def system_prompt_extension(self) -> str: ...

    @property
    @abstractmethod
    def director_prompt(self) -> str:
        """System prompt for the director LLM. Each mode writes its own."""
        ...

    @staticmethod
    def init_extra() -> dict:
        """Optional initial extra state. Override to set mode-specific defaults."""
        return {"best_take": ""}

    max_turns: int

    # ── Director ────────────────────────────────────────────────────────────

    async def _direct(self, state: RoastState, records: list) -> dict:
        """Run the director LLM to evaluate the conversation.

        Returns: {"action": "none"|"inject", "best_take": str|null,
                  "prompt": str|null, "close": bool}
        On failure, returns action="none" gracefully.
        """
        from agent_config import get_config
        from core.llm import get_llm
        from core.llm.types import Message as LLMMessage

        director_model = get_config().DIRECTOR_MODEL

        # Build director messages: system prompt + current roast conversation only
        messages = [LLMMessage.system(self.director_prompt)]
        roast_id = state.roast_instance_id
        has_roast_body = False

        for r in records:
            role = getattr(r, "role", "")
            content = getattr(r, "content", "")
            if role not in ("system", "user", "assistant", "tool") or not content:
                continue
            # Match by roast_instance_id when present. Messages with
            # rid=None (e.g. current LLM turn, not yet persisted) are
            # always included — they belong to the current roast.
            # Match by roast_instance_id. Loaded records have IDs from
            # sync _assign_roast_instance_id. Runner messages have rid=None
            # (not yet persisted) but ARE the current roast — include them.
            rid = getattr(r, "roast_instance_id", None)
            is_roast_body = (
                role == "system"
                and ROAST_BODY_PREFIX in content
                and not has_roast_body
            )
            if rid is not None and rid != roast_id and not is_roast_body:
                continue
            if is_roast_body:
                has_roast_body = True
            messages.append(LLMMessage(role=role, content=content))  # type: ignore[arg-type]

        try:
            llm = get_llm(director_model)
            response = await llm.chat(
                messages,
                model=director_model,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "director_output",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["none", "inject"]},
                                "best_take": {
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "null"},
                                    ]
                                },
                                "prompt": {
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "null"},
                                    ]
                                },
                                "close": {"type": "boolean"},
                            },
                            "required": ["action", "best_take", "prompt", "close"],
                            "additionalProperties": False,
                        },
                    },
                },
            )

            from utils import safe_parse_llm_json
            result = safe_parse_llm_json(response.content)
            logger.info(
                f"[{self.mode}] Director: action={result.get('action')} "
                f"best_take={'yes' if result.get('best_take') else 'no'} "
                f"close={result.get('close', False)}"
            )
            # Fire-and-forget: persist Director decision for debugging/analysis
            asyncio.ensure_future(_write_director_log(
                state.roast_instance_id, state.turn_count, result,
            ))
            return result
        except Exception as e:
            logger.error(f"[{self.mode}] Director failed: {e}")
            return {"action": "none", "best_take": None, "prompt": None, "close": False}

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
        records: list,
        redis,
        pg_pool=None,
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
            director_result = await self._direct(state, records)

            if director_result.get("best_take"):
                state.extra["best_take"] = director_result["best_take"]

            # Inject: Director wants to give the Agent a hint (→ [Game Event] in
            # next turn's messages, NOT persisted to agent_conversations).
            if director_result.get("action") == "inject" and director_result.get("prompt"):
                director_prompt = director_result["prompt"]

            # Close: Director signals the game should end (→ phase=CLOSING
            # persisted in RoastState, affects all future turns).
            if director_result.get("close"):
                state.phase = Phase.CLOSING
                # Ensure a closing prompt is always injected so the Agent
                # sees [Game Event] and knows to wrap up.
                if not director_prompt:
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
                if trigger.check(state, records):
                    prompt = trigger.prompt(state) if callable(trigger.prompt) else trigger.prompt
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
    """Persist Director LLM decision to roast_director_logs for analysis."""
    try:
        from bootstrap.factory import get_pg_pool
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roast_director_logs
                   (roast_instance_id, turn_number, action, best_take, prompt, close)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                roast_instance_id,
                turn_number,
                result.get("action"),
                result.get("best_take"),
                result.get("prompt"),
                result.get("close", False),
            )
    except Exception as e:
        logger.warning(f"[Director] Failed to write director log: {e}")

