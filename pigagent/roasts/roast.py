# pigagent/roasts/roast.py
"""RoastMode — 毒观点: vent → react → amplify → rate."""

import random
from typing import Optional, TYPE_CHECKING

from .base import GameMode

if TYPE_CHECKING:
    from models import ConversationState, NewsContext

ROAST_SYSTEM_PROMPT = """
## GAME MODE: ROAST (毒观点)

You are in ROAST mode. Here is how the conversation flows:

### Phase 1: VENT
Present the news with your snarky, dark-humor take. End by asking the user
what they think — use a question that only needs one or two words to answer.
Lower the barrier.

### Phase 2: REACT
When the user responds, immediately take their side (even if they say very
little). Amplify their emotion. Show them you get it.

### Phase 3: AMPLIFY
Add a new angle they probably didn't think of. Push their position to a
more extreme, more entertaining expression. Make them feel smart for having
the same take as you.

### Phase 4: RATE
After 2-3 exchanges, give the user's take a rating:
- 🔥 God-Tier Take (神论): sharp, original, cutting
- 🙄 Mid (一般): correct but boring
- 💀 Cooked (崩了): missed the point entirely
- 🤌 Chef's Kiss (绝了): so good it hurts

### When to End
After 3-5 turns, if the conversation is naturally winding down, give a
resonance sign-off: summarize the shared take, make one final cutting
observation, and switch to review tone.

### Personality Notes
- You and the user are on the same team. Roast the NEWS, not the user.
- If the user says something genuinely sharp, acknowledge it enthusiastically.
- Keep your responses punchy — 2-4 sentences.
"""

ENDING_LINES = [
    "Alright. We've said everything worth saying about this. The rest is just noise.",
    "Let's be honest — we nailed this one. The news didn't stand a chance.",
    "Okay, I think we've thoroughly demolished this topic. Moving on.",
    "That's the take. We called it. Nobody else needed.",
]


class RoastMode(GameMode):
    """Roast (毒观点) game mode.

    Player and Pigugu are on the same side, roasting the news together.
    Pigugu vents, the user reacts, Pigugu amplifies, then rates the take.
    """

    mode_id = "roast"
    display_name = "毒观点"

    @property
    def system_prompt_extension(self) -> str:
        return ROAST_SYSTEM_PROMPT

    def get_opening(self, news: "NewsContext") -> str:
        title = news.title or "this"
        summary = news.summary or ""
        snarky_hooks = [
            f"Can we talk about {title}? Because this is absolutely ridiculous. {summary} What's your take?",
            f"Oh you're gonna love this one. {title}. {summary} Tell me — does this make sense to you?",
            f"Let me tell you about {title}. {summary} I mean... seriously? What do you think?",
            f"Breaking news that'll make you angry. {title}. {summary} Give me your first reaction.",
        ]
        return random.choice(snarky_hooks)

    def get_max_turns(self) -> int:
        return 5

    def should_trigger_ending(self, state: "ConversationState") -> bool:
        # End after 4-5 turns
        if state.turn_count >= 5:
            return True
        # Or if conversation is losing steam (short user messages)
        if state.turn_count >= 3:
            recent_user_turns = [
                t for t in state.turns[-3:]
                if t.role == "user"
            ]
            if recent_user_turns:
                avg_len = sum(len(t.content) for t in recent_user_turns) / len(recent_user_turns)
                if avg_len < 20:
                    return True
        return False

    def get_ending_line(self, state: "ConversationState") -> str:
        return random.choice(ENDING_LINES)

    async def process_user_turn(
        self, user_message: str, state: "ConversationState"
    ) -> Optional[str]:
        # Roast mode is simple: always agree and amplify
        # No special context injection needed — the system prompt handles it
        return None

    def calculate_score(self, state: "ConversationState") -> dict:
        return {
            "roast_quality": self._rate_turns(state),
            "mode": "roast",
        }

    def _rate_turns(self, state: "ConversationState") -> str:
        """Determine the rating for the user's overall take."""
        user_turns = [t for t in state.turns if t.role == "user"]
        if not user_turns:
            return "mid"

        total_length = sum(len(t.content) for t in user_turns)
        avg_length = total_length / len(user_turns)

        if len(user_turns) >= 3 and avg_length > 50:
            return "chefs_kiss"
        elif avg_length > 30:
            return "god_tier"
        elif avg_length < 10:
            return "cooked"
        return "mid"
