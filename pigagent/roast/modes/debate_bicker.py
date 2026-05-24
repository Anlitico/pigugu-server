"""DebateBicker — 辩论抬杠: Pigugu picks a controversial side; the user argues back."""

from __future__ import annotations

from typing import TYPE_CHECKING

from model import Mode
from roast.base import GameMode, Trigger

if TYPE_CHECKING:
    from roast.state import RoastState

DEBATE_SYSTEM_PROMPT = """
## GAME MODE: DEBATE BICKER (辩论抬杠)

Pigugu picks a side — probably not yours. You argue back.
You get the last word. It ends with a fart.

### Rules
- Take a strong, controversial stance. Be provocative but playful.
- Challenge the user's arguments aggressively but with humor.
- The user MUST have the last word — never end on your own point.
- When you sense the user is making their final argument, concede gracefully.

### Ending
The debate ends when the user clearly wins (3+ strong points), after 6 turns,
or when the user repeats themselves. Pigugu responds with a FART SOUND —
different fart types for different outcomes:
- Short, loud fart = "Alright, you got me."
- Long, low fart = "I still disagree but fine."
- Rapid-fire farts = "You make too much sense, I'm speechless."
The user should feel satisfied and entertained, never frustrated.
"""


class DebateBickerMode(GameMode):
    mode = Mode.DEBATE_BICKER
    display_name = "辩论抬杠"
    max_turns = 6

    @property
    def system_prompt_extension(self) -> str:
        return DEBATE_SYSTEM_PROMPT

    @property
    def triggers(self) -> list[Trigger]:
        return [
            Trigger(
                name="ending_max_turns",
                check=lambda s, r: s.turn_count >= self.max_turns,
                prompt=(
                    "THE DEBATE HAS REACHED ITS LIMIT. The user gets the last word.\n"
                    "You are now in REVIEW TONE. Concede with a FART SOUND — "
                    "pick the right fart type for the outcome. Make sure the user "
                    "feels satisfied, not frustrated."
                ),
            ),
            Trigger(
                name="user_won",
                check=lambda s, r: (
                    s.extra.get("strong_points", 0) >= 3
                    and s.turn_count >= 4
                ),
                prompt=(
                    "The user has clearly won this debate with strong arguments. "
                    "Acknowledge their victory with a RAPID-FIRE FART — "
                    "you're impressed and speechless. Let them enjoy the win."
                ),
            ),
            Trigger(
                name="user_repeat",
                check=lambda s, r: _detect_repeat(r),
                prompt=(
                    "The user is repeating the same argument. The debate has run "
                    "its course. Let the user have the last word and respond with "
                    "a SHORT LOUD FART — you concede but move on."
                ),
            ),
        ]

    def score(self, state: RoastState) -> dict:
        strong = state.extra.get("strong_points", 0)
        if strong >= 3:
            result = "user_win"
        elif strong >= 1:
            result = "draw"
        else:
            result = "agent_win"
        return {"mode": str(self.mode), "result": result, "strong_points": strong}


def _detect_repeat(records: list) -> bool:
    user_turns = [r for r in records[-4:]
                  if getattr(r, "role", "") == "user"]
    if len(user_turns) >= 2:
        a = getattr(user_turns[-1], "content", "").strip().lower()
        b = getattr(user_turns[-2], "content", "").strip().lower()
        return bool(a and a == b)
    return False
