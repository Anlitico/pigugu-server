"""GameMode ABC and Trigger  -  abstract interface for roast game modes."""

from __future__ import annotations

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
        from config import get_config
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
            # Match by roast_instance_id. Records from the current roast
            # always have their roast_instance_id set (sync assignment).
            rid = getattr(r, "roast_instance_id", None)
            is_roast_body = (
                role == "system"
                and ROAST_BODY_PREFIX in content
                and not has_roast_body
            )
            if rid != roast_id and not is_roast_body:
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
        try:
            director_result = await self._direct(state, records)

            if director_result.get("best_take"):
                state.extra["best_take"] = director_result["best_take"]

            if director_result.get("action") == "inject" and director_result.get("prompt"):
                prompt = director_result["prompt"]
                await pending.write(state.roast_instance_id, prompt, redis)

                if director_result.get("close"):
                    state.phase = Phase.CLOSING
                    logger.info(
                        f"[{self.mode}] Director triggered CLOSING "
                        f"roast={state.roast_instance_id} turn={state.turn_count}"
                    )

                await state.save(redis, pg_pool)
                return prompt
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

