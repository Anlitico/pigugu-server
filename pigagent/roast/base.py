"""GameMode ABC and Trigger — abstract interface for roast game modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, ClassVar, TYPE_CHECKING

from loguru import logger
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

    If affects_phase is True, the state phase is updated to REVIEW
    when this trigger fires. Used for game-ending triggers.
    """

    name: str
    check: Callable[[RoastState, list], bool] = field(repr=False)
    prompt: str | Callable[[RoastState], str] = field(repr=False)
    affects_phase: bool = False


class GameMode(ABC):
    """Abstract base for a roast game mode.

    Subclasses define their own rules, triggers, and scoring logic.
    Each mode can override tick() for custom advance behavior.
    """

    mode: Mode

    @property
    @abstractmethod
    def system_prompt_extension(self) -> str: ...

    max_turns: int

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
                    "THE GAME IS OVER. You are now in REVIEW TONE.\n"
                    "Wrap up with a closing thought. Stay in character."
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
        """Advance state after one user turn. Default implementation.

        1. Bump turn count
        2. Iterate triggers (first match wins)
        3. On match: write prompt to Redis, update phase if needed
        4. Persist updated state

        Override for mode-specific behavior (e.g. update extra before checking).

        Returns the prompt string if a trigger fired, else None.
        """
        if state.phase != Phase.ACTIVE:
            return None

        state.turn_count += 1

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
            state.phase = Phase.REVIEW

        logger.info(
            f"[{self.mode}] Triggered: {trigger.name} "
            f"roast={state.roast_instance_id} turn={state.turn_count}"
        )

    # ── Scoring ─────────────────────────────────────────────────────────

    def score(self, state: RoastState) -> dict:
        """Calculate mode-specific scores."""
        return {"mode": str(self.mode)}
