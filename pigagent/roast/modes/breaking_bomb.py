"""BreakingBomb — breaking news just dropped, get the user's gut reaction.

State (extra):
    reactions  -  [{turn, text, timestamp}, ...]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from roast.types import Mode
from roast.base import GameMode, Trigger
from roast.prompts import render

if TYPE_CHECKING:
    from roast.state import RoastState


class BreakingBombMode(GameMode):
    mode = Mode.BREAKING_BOMB
    max_turns = 3

    @property
    def system_prompt_extension(self) -> str:
        return render("breaking_bomb_system")

    @property
    def director_prompt(self) -> str:
        return render("breaking_bomb_director")

    # ── State helpers ──────────────────────────────────────────────────

    @staticmethod
    def init_extra() -> dict:
        return {"reactions": [], "best_take": ""}

    # ── Advance ────────────────────────────────────────────────────────

    async def tick(
        self,
        state: RoastState,
        *,
        records: list,
        redis,
        pg_pool=None,
    ) -> str | None:
        """Record the user's reaction, then run base trigger checks."""
        self._update_state(state, records)
        return await super().tick(state, records=records, redis=redis, pg_pool=pg_pool)

    def _update_state(self, state: RoastState, records: list) -> None:
        """Record the latest user reaction."""
        user_turns = [r for r in records
                      if getattr(r, "role", "") == "user"]
        if not user_turns:
            return

        latest = user_turns[-1]
        state.extra.setdefault("reactions", []).append({
            "turn": state.turn_count,
            "text": getattr(latest, "content", "")[:200],
        })

    # ── Triggers ───────────────────────────────────────────────────────

    @property
    def triggers(self) -> list[Trigger]:
        return [
            Trigger(
                name="ending_max_turns",
                check=lambda s, r: s.turn_count >= self.max_turns,
                prompt=render("breaking_bomb_ending"),
                affects_phase=True,
            ),
        ]

    def score(self, state: RoastState) -> dict:
        reactions = state.extra.get("reactions", [])
        return {"mode": str(self.mode), "reaction_count": len(reactions)}
