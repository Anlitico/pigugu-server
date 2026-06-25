"""RoastTogether — roast a hot topic together with the user.

Uses a director LLM to evaluate the conversation every turn and inject
guidance to the actor LLM when needed. The actor focuses on natural
banter; the director handles pacing, escalation, and closing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from roast.types import Mode
from roast.base import GameMode, Trigger

if TYPE_CHECKING:
    from roast.state import RoastState
    from prompts import PromptStore


class RoastTogetherMode(GameMode):
    mode = Mode.ROAST_TOGETHER
    max_turns = 50

    async def get_system_prompt_extension(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("roast_together_system")

    async def get_director_prompt(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("roast_together_director")

    @staticmethod
    def init_extra() -> dict:
        return {"settled": False, "best_take": ""}

    # ── Triggers ───────────────────────────────────────────────────────

    @property
    def triggers(self) -> list[Trigger]:
        return [
            Trigger(
                name="ending_max_turns",
                check=lambda s, r: s.turn_count >= self.max_turns,
                prompt=lambda s, ps: ps.render(
                    "roast_together_ending",
                    best_take=s.extra.get("best_take", ""),
                ),
                affects_phase=True,
            ),
        ]

    def score(self, state: RoastState) -> dict:
        return {
            "mode": str(self.mode),
            "turns": state.turn_count,
            "settled": state.extra.get("settled", False),
            "best_take": state.extra.get("best_take", ""),
        }
