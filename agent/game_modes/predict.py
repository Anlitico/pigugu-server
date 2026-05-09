# agent/game_modes/predict.py
"""PredictMode — 预测混乱: predict → wait → reveal."""

import random
from typing import Optional, TYPE_CHECKING

from .base import GameMode

if TYPE_CHECKING:
    from models import ConversationState, NewsContext

PREDICT_SYSTEM_PROMPT = """
## GAME MODE: PREDICT (预测混乱)

You are in PREDICT mode. The user will make a prediction about how a news
story will unfold over the next 24-72 hours.

### Phase 1: SET THE STAGE
Present the news with all the relevant variables. Highlight what's uncertain.
Don't take a position — just lay out the battlefield. Then ask the user:
"What's your prediction?"

### Phase 2: PROBE
When the user makes a prediction, push them to be specific. "How confident?"
"What would change your mind?" "What's the one variable that matters most?"

### Phase 3: RECORD
Acknowledge the prediction clearly. Say you'll check back in 24-72 hours.
Be slightly ominous: "I'll remember this."

### When to Check Back
End the conversation after 3-4 turns. The prediction is recorded. The reveal
happens in a FUTURE session when results are available.

### Personality Notes
- Be a gracious host. This is a game show, not a fight.
- Respect bold predictions. "That's a spicy one. I like it."
- Never mock the prediction itself — the mockery comes LATER if they're wrong.
- Keep it light and fun. 2-3 sentences.
"""

PREDICT_OPENINGS = [
    "I want you to predict something. Here's the situation: {title}. {summary} What happens next?",
    "Let's play a game. {title}. {summary} I'm giving you 72 hours. What's your call?",
    "Time to put your foresight on the line. {title}. {summary} Tell me what happens.",
]


class PredictMode(GameMode):
    """Predict (预测混乱) game mode.

    User makes a prediction about news outcome. Pigugu records it.
    Reveal happens in a future session when results are available.
    """

    mode_id = "predict"
    display_name = "预测混乱"

    @property
    def system_prompt_extension(self) -> str:
        return PREDICT_SYSTEM_PROMPT

    def get_opening(self, news: "NewsContext") -> str:
        title = news.title or "this situation"
        summary = news.summary or ""
        template = random.choice(PREDICT_OPENINGS)
        return template.format(title=title, summary=summary)

    def get_max_turns(self) -> int:
        return 4

    def should_trigger_ending(self, state: "ConversationState") -> bool:
        return state.turn_count >= self.get_max_turns()

    def get_ending_line(self, state: "ConversationState") -> str:
        predictions = state.custom.get("predictions", [])
        if predictions:
            return (
                f"Alright. Your prediction is locked in. "
                f"I'll check back when we know the outcome. "
                f"Don't change your story when I come back."
            )
        return "Alright. I wanted a prediction, but you played it safe. Next time, take a swing."

    async def process_user_turn(
        self, user_message: str, state: "ConversationState"
    ) -> Optional[str]:
        text = user_message.strip()
        state.custom.setdefault("predictions", [])
        state.custom["predictions"].append({
            "turn": state.turn_count,
            "text": text[:200],
        })
        return None

    def calculate_score(self, state: "ConversationState") -> dict:
        predictions = state.custom.get("predictions", [])
        return {
            "prediction_count": len(predictions),
            "prediction_length": sum(len(p["text"]) for p in predictions),
            "mode": "predict",
        }
