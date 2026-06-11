"""RoastTogether — roast a hot topic together with the user.

The LLM handles all behavioral decisions (energy, escalation, best-take, ending)
via the system prompt. This module only provides the safety-net trigger.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from roast.types import Mode
from roast.base import GameMode, Trigger
from roast.prompts import render

if TYPE_CHECKING:
    from roast.state import RoastState


class RoastTogetherMode(GameMode):
    mode = Mode.ROAST_TOGETHER
    max_turns = 8

    @property
    def system_prompt_extension(self) -> str:
        return render("roast_together_system")

    @staticmethod
    def init_extra() -> dict:
        return {"settled": False}

    # ── Triggers ───────────────────────────────────────────────────────

    @property
    def triggers(self) -> list[Trigger]:
        return [
            Trigger(
                name="ending_max_turns",
                check=lambda s, r: s.turn_count >= self.max_turns,
                prompt=render("roast_together_ending"),
                affects_phase=True,
            ),
        ]

    def score(self, state: RoastState) -> dict:
        return {
            "mode": str(self.mode),
            "turns": state.turn_count,
            "settled": state.extra.get("settled", False),
        }
