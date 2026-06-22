"""BreakingBomb — breaking news just dropped, get the user's gut reaction.

State (extra):
    reactions  -  [{turn, text, timestamp}, ...]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from roast.types import Mode
from roast.base import GameMode, Trigger

if TYPE_CHECKING:
    from roast.state import RoastState
    from prompts import PromptStore


class BreakingBombMode(GameMode):
    mode = Mode.BREAKING_BOMB
    max_turns = 3

    async def get_system_prompt_extension(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("breaking_bomb_system")

    async def get_director_prompt(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("breaking_bomb_director")

    # ── State helpers ──────────────────────────────────────────────────

    @staticmethod
    def init_extra() -> dict:
        return {"reactions": [], "best_take": ""}

    # ── Advance ────────────────────────────────────────────────────────

    async def tick(
        self,
        state: RoastState,
        *,
        wc,
        redis,
        pg_pool=None,
        current_msg=None,
        prompt_store: PromptStore | None = None,
    ) -> str | None:
        """Record the user's reaction, then run base trigger checks."""
        self._update_state(state, wc.raw_records)
        return await super().tick(
            state, wc=wc, redis=redis, pg_pool=pg_pool,
            current_msg=current_msg, prompt_store=prompt_store,
        )

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
                prompt=lambda _s, ps: ps.get("breaking_bomb_ending"),
                affects_phase=True,
            ),
        ]

    def score(self, state: RoastState) -> dict:
        reactions = state.extra.get("reactions", [])
        return {"mode": str(self.mode), "reaction_count": len(reactions)}
