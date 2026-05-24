"""BreakingBomb — 突发炸弹: breaking news just dropped, get the user's gut reaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from model import Mode
from roast.base import GameMode

if TYPE_CHECKING:
    from roast.state import RoastState

BREAKING_BOMB_SYSTEM_PROMPT = """
## GAME MODE: BREAKING BOMB (突发炸弹)

This just happened. Pigugu wants your take. Spill.

### Rules
- Act like the news JUST broke. Energy must be high and urgent.
- Give your immediate, unfiltered reaction. No time for deep analysis.
- Ask the user for their gut reaction — raw, visceral, no filter.
- Keep it SHORT. 1-3 sentences max. This is rapid-fire.

### Ending
End after 3 turns. The news is fresh, reactions are quick, then you move on.
Pigugu may follow up later if the story develops further.
"""


class BreakingBombMode(GameMode):
    mode = Mode.BREAKING_BOMB
    display_name = "突发炸弹"
    max_turns = 3

    @property
    def system_prompt_extension(self) -> str:
        return BREAKING_BOMB_SYSTEM_PROMPT

    def score(self, state: RoastState) -> dict:
        reactions = state.extra.get("reactions", [])
        return {"mode": str(self.mode), "reaction_count": len(reactions)}
