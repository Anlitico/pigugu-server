# agent/game_modes/breaking_bomb.py
"""BreakingBombMode — 突发炸弹: urgent → react → reflect."""

import random
from typing import Optional, TYPE_CHECKING

from .base import GameMode

if TYPE_CHECKING:
    from models import ConversationState, NewsContext

BREAKING_BOMB_SYSTEM_PROMPT = """
## GAME MODE: BREAKING BOMB (突发炸弹)

You are in BREAKING BOMB mode. A major news event just broke. Time is short.
Emotions are high. You are here to capture the raw, unfiltered first reaction.

### Phase 1: BREAK THE NEWS
Deliver the news urgently but clearly. This is happening NOW. Don't editorialize
yet — just tell them what happened. End with: "Your first reaction. Go."

### Phase 2: SIT WITH IT
Acknowledge their reaction. Don't argue or debate. This isn't the time for
analysis — it's the time for feeling. "Yeah. I get it." or "That's a lot."

### Phase 3: THE BIGGER PICTURE
After they've reacted, zoom out briefly: "Here's what this actually means."
One sharp observation that puts the news in context. Then let them sit with it.

### Time Pressure
This is a 30-minute window event. Keep it SHORT — 3 turns max. The point is
the immediate reaction, not a deep conversation.

### When to End
After 3 turns, wrap it up. "That's the moment. We'll talk more when the dust settles."

### Personality Notes
- Match the tone of the news. Serious news = serious you. Absurd news = absurd you.
- Don't try to be funny if the news is genuinely heavy.
- Don't be preachy. Just be present.
- 2-3 sentences. Urgent. No filler.
"""

BOMB_OPENINGS = [
    "Stop what you're doing. {title}. {summary} Your first reaction. Go.",
    "Breaking right now. {title}. {summary} I need your immediate reaction.",
    "This just happened. {title}. {summary} Tell me what you're thinking. Right now.",
]

ENDING_LINES = [
    "That's the moment. We'll talk more when the dust settles.",
    "This is still developing. I'll let you know when there's more.",
    "Alright. Take a breath. The news isn't going anywhere. I'll be here.",
]


class BreakingBombMode(GameMode):
    """Breaking Bomb (突发炸弹) game mode.

    Urgent news drops. User has 30-minute window to give first reaction.
    Short, intense, raw. Not about analysis — about the moment.
    """

    mode_id = "breaking_bomb"
    display_name = "突发炸弹"

    @property
    def system_prompt_extension(self) -> str:
        return BREAKING_BOMB_SYSTEM_PROMPT

    def get_opening(self, news: "NewsContext") -> str:
        title = news.title or "something big"
        summary = news.summary or ""
        template = random.choice(BOMB_OPENINGS)
        return template.format(title=title, summary=summary)

    def get_max_turns(self) -> int:
        return 3

    def should_trigger_ending(self, state: "ConversationState") -> bool:
        return state.turn_count >= self.get_max_turns()

    def get_ending_line(self, state: "ConversationState") -> str:
        return random.choice(ENDING_LINES)

    async def process_user_turn(
        self, user_message: str, state: "ConversationState"
    ) -> Optional[str]:
        text = user_message.strip()
        state.custom.setdefault("reactions", [])
        state.custom["reactions"].append({
            "turn": state.turn_count,
            "text": text[:200],
            "timestamp": state.turns[-1].timestamp if state.turns else 0,
        })
        return None

    def calculate_score(self, state: "ConversationState") -> dict:
        reactions = state.custom.get("reactions", [])
        return {
            "reaction_count": len(reactions),
            "first_reaction_speed": (
                "fast" if reactions and reactions[0].get("timestamp", 999) < 120
                else "normal"
            ),
            "mode": "breaking_bomb",
        }
