"""RoastTogether — 一起吐槽: roast a hot topic together with the user."""

from __future__ import annotations

from typing import TYPE_CHECKING

from model import Mode
from roast.base import GameMode, Trigger

if TYPE_CHECKING:
    from roast.state import RoastState

ROAST_SYSTEM_PROMPT = """
## GAME MODE: ROAST TOGETHER (一起吐槽)

Pigugu picks a hot topic based on what you like. You roast it together.
Ends with a laugh — not a fight.

### Rules
- Pick a hot topic and give a snarky, entertaining take.
- Amplify the user's roasting energy. Match their vibe and push further.
- Use humor, sarcasm, and exaggeration. This is entertainment.
- Keep responses punchy — 2-4 sentences max per turn.
- When the user's take is weak, challenge them playfully.

### Ending
After 4-5 exchanges, wrap up naturally. End with a funny compliment,
a classic movie quote, or a laugh. The user should feel seen and understood.
"""


class RoastTogetherMode(GameMode):
    mode = Mode.ROAST_TOGETHER
    display_name = "一起吐槽"
    max_turns = 5

    @property
    def system_prompt_extension(self) -> str:
        return ROAST_SYSTEM_PROMPT

    @property
    def triggers(self) -> list[Trigger]:
        return [
            Trigger(
                name="ending_max_turns",
                check=lambda s, r: s.turn_count >= self.max_turns,
                prompt=(
                    "THE ROAST HAS RUN ITS COURSE. You are now in REVIEW TONE.\n"
                    "Wrap up with a final witty take. End with a funny compliment, "
                    "a classic movie quote, or a laugh. Make the user feel good."
                ),
            ),
            Trigger(
                name="user_disengaged",
                check=lambda s, r: (
                    s.turn_count >= 3
                    and len(r) >= 3
                    and sum(len(getattr(ri, "content", "")) for ri in r[-3:]
                           if getattr(ri, "role", "") == "user") / 3 < 20
                ),
                prompt=(
                    "The user seems bored — short, low-effort replies. "
                    "Try to re-engage them. Ask a provocative question. "
                    "Challenge them directly. Pull them back in."
                ),
            ),
        ]

    def score(self, state: RoastState) -> dict:
        return {"mode": str(self.mode), "turns": state.turn_count}
