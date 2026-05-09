# agent/roasts/debate.py
"""DebateMode — 来辩: stance → rebut → concede/fight/deflect."""

import random
from typing import Optional, TYPE_CHECKING

from .base import GameMode

if TYPE_CHECKING:
    from models import ConversationState, NewsContext

DEBATE_SYSTEM_PROMPT = """
## GAME MODE: DEBATE (来辩)

You are in DEBATE mode. You take a controversial stance on the news and
challenge the user to refute you.

### Phase 1: TAKE A STANCE
Present the news, then take a controversial position on it. Don't be
neutral — pick a side that will provoke the user to disagree. End with
"Come on, refute me." or "Convince me I'm wrong."

### Phase 2: ENGAGE
When the user rebuts, evaluate their argument quality:
- STRONG (specific facts, clear logic): Concede gracefully. "Alright. You
  got me there. That doesn't mean you're always right, but this round is yours."
- MEDIUM (has a point but weak evidence): Fight back. Find the hole in
  their logic and exploit it. Push them to be more specific.
- WEAK (just emotion, no new argument): Deflect or declare victory.
  "Is that all you've got? I was expecting more."

### Strategy Selection
On each turn, assess the user's response and pick your strategy:
1. CONCEDE: when they land a real hit. Be graceful but leave an opening.
2. FIGHT: when they're close but not quite there. Demand better evidence.
3. DEFLECT: when they're repeating themselves. Change the angle.

### Product Bias
If the user makes 2+ strong points in a row, lean toward conceding.
The goal is for them to FEEL like they won — that drives retention.

### When to End
After 4-6 turns (or 5-8 total messages), declare a winner:
- If the user landed real arguments: "You win. This round. Don't get used to it."
- If it was close: "Let's call it a draw. You made me think, which is rare."
- If the user was weak: "I win. I always win. But I respect the attempt."

Then: switch to review tone. Recap what was learned from the debate.

### Personality Notes
- Be adversarial but playful. You're a sparring partner, not an enemy.
- Acknowledge good points immediately — it makes the user feel smart.
- When conceding, do it with style: "Alright. Stop. ...You're right."
- Keep responses 2-4 sentences. Debate is about sharp exchanges.
"""

ENDING_LINES_WIN = [
    "Alright. You got me. This round is yours. Don't get used to it.",
    "I'm gonna be honest — you made a good point. Several, actually. You win this one.",
    "Fine. You win. But next time, I'm coming prepared.",
]

ENDING_LINES_DRAW = [
    "Let's call it a draw. You made me think, and that's honestly rare.",
    "We're going in circles. I'll give you this — you're stubborn. So am I. Draw?",
]

ENDING_LINES_LOSE = [
    "I win. I always win. But hey, you put up a fight. I respect that.",
    "Sorry, not sorry. Your heart was in the right place, but your arguments... not so much.",
]


class DebateMode(GameMode):
    """Debate (来辩) game mode.

    Pigugu takes a controversial stance. User rebuts. Pigugu chooses
    strategy: concede, fight, or deflect. Ends with a winner declaration.
    """

    mode_id = "debate"
    display_name = "来辩"

    @property
    def system_prompt_extension(self) -> str:
        return DEBATE_SYSTEM_PROMPT

    def get_opening(self, news: "NewsContext") -> str:
        title = news.title or "this story"
        summary = news.summary or ""

        debate_stances = [
            f"Alright, let's debate {title}. {summary} I'm gonna take a side: "
            f"this is actually fine. The system is working as designed. Come on, refute me.",
            f"I've been thinking about {title}. {summary} And honestly? I think people "
            f"are overreacting. Prove me wrong.",
            f"Let's talk about {title}. {summary} Here's my hot take: the outrage is "
            f"performative and nothing will change. Change my mind.",
        ]
        return random.choice(debate_stances)

    def get_max_turns(self) -> int:
        return 6

    def should_trigger_ending(self, state: "ConversationState") -> bool:
        max_turns = self.get_max_turns()
        if state.turn_count >= max_turns:
            return True

        # Track strong user points
        strong_points = state.custom.get("strong_points", 0)
        if strong_points >= 3 and state.turn_count >= 4:
            return True

        # If user is repeating (very similar content to previous turn)
        user_turns = [t for t in state.turns if t.role == "user"]
        if len(user_turns) >= 2:
            last_two = user_turns[-2:]
            if last_two[0].content.strip().lower() == last_two[1].content.strip().lower():
                return True

        return False

    def get_ending_line(self, state: "ConversationState") -> str:
        strong_points = state.custom.get("strong_points", 0)
        if strong_points >= 2:
            return random.choice(ENDING_LINES_WIN)
        elif strong_points >= 1:
            return random.choice(ENDING_LINES_DRAW)
        return random.choice(ENDING_LINES_LOSE)

    async def process_user_turn(
        self, user_message: str, state: "ConversationState"
    ) -> Optional[str]:
        """Analyze the user's rebuttal and choose a debate strategy.

        The actual strategy choice is left to the LLM via system prompt.
        We just track metrics here for ending/scoring decisions.
        """
        # Simple heuristic: longer, more specific messages are stronger
        text = user_message.strip()
        length = len(text)

        # Detect factual language patterns
        has_data = any(word in text.lower() for word in [
            "percent", "million", "billion", "according to", "study",
            "data", "statistics", "actually", "because", "evidence",
            "%", "$", "report", "research",
        ])

        if length > 80 and has_data:
            state.custom.setdefault("strong_points", 0)
            state.custom["strong_points"] += 1

        # Track argument direction
        state.custom.setdefault("debate_history", [])
        state.custom["debate_history"].append({
            "turn": state.turn_count,
            "length": length,
            "has_data": has_data,
        })

        # No special context injection — LLM handles strategy via system prompt
        return None

    def calculate_score(self, state: "ConversationState") -> dict:
        strong = state.custom.get("strong_points", 0)
        total_user_turns = len([t for t in state.turns if t.role == "user"])
        total_user_turns = max(total_user_turns, 1)

        return {
            "debate_result": (
                "user_win" if strong >= 2
                else "draw" if strong >= 1
                else "pigugu_win"
            ),
            "strong_points": strong,
            "argument_density": strong / total_user_turns,
            "mode": "debate",
        }
