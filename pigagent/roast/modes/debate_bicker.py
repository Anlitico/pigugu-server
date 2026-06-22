"""DebateBicker — Pigugu picks a controversial side; the user argues back.

State (extra):
    strong_points   -  count of data-backed arguments from the user
    fart_type       -  concede | grudging | impressed (set on ending)
    debate_history  -  [{turn, length, has_data}, ...]

Per PRD §6.2: user MUST have the last word. Pigugu responds with a fart sound.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from roast.types import Mode
from roast.base import GameMode, Trigger

if TYPE_CHECKING:
    from roast.state import RoastState
    from prompts import PromptStore

# ── Strong point detection ───────────────────────────────────────────────

_DATA_KEYWORDS = [
    "percent", "million", "billion", "according to", "study",
    "data", "statistics", "actually", "because", "evidence",
    "%", "$", "report", "research", "numbers", "fact",
    "proved", "shows", "proves", "confirmed",
]


def _is_strong_point(text: str) -> bool:
    if len(text) < 80:
        return False
    lower = text.lower()
    return any(kw in lower for kw in _DATA_KEYWORDS)


class DebateBickerMode(GameMode):
    mode = Mode.DEBATE_BICKER
    max_turns = 6

    async def get_system_prompt_extension(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("debate_bicker_system")

    async def get_director_prompt(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("debate_bicker_director")

    # ── State helpers ──────────────────────────────────────────────────

    @staticmethod
    def init_extra() -> dict:
        return {
            "strong_points": 0,
            "fart_type": "",
            "debate_history": [],
            "best_take": "",
        }

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
        """Update strong points, then run base trigger checks."""
        self._update_state(state, wc.raw_records)
        return await super().tick(
            state, wc=wc, redis=redis, pg_pool=pg_pool,
            current_msg=current_msg, prompt_store=prompt_store,
        )

    def _update_state(self, state: RoastState, records: list) -> None:
        """Detect strong points in the latest user turn."""
        user_turns = [r for r in records
                      if getattr(r, "role", "") == "user"]
        if not user_turns:
            return

        latest = user_turns[-1]
        text = getattr(latest, "content", "")
        length = len(text)
        has_data = _is_strong_point(text)

        if has_data:
            state.extra["strong_points"] = state.extra.get("strong_points", 0) + 1

        state.extra.setdefault("debate_history", []).append({
            "turn": state.turn_count,
            "length": length,
            "has_data": has_data,
        })

    # ── Triggers ───────────────────────────────────────────────────────

    @property
    def triggers(self) -> list[Trigger]:
        return [
            Trigger(
                name="user_won",
                check=lambda s, r: (
                    s.extra.get("strong_points", 0) >= 3
                    and s.turn_count >= 4
                ),
                prompt=lambda s, ps: ps.render(
                    "debate_bicker_user_won",
                    fart_impressed=(
                        "RAPID-FIRE FART. You're genuinely impressed  -  "
                        "'You make too much sense. I'm speechless.'"
                    ),
                    strong_points=s.extra.get("strong_points", 0),
                    turn_count=s.turn_count,
                ),
            ),
            Trigger(
                name="ending_max_turns",
                check=lambda s, r: s.turn_count >= self.max_turns,
                prompt=lambda s, ps: ps.render(
                    "debate_bicker_ending",
                    fart_type=(
                        "RAPID-FIRE FART. You're genuinely impressed."
                        if s.extra.get("strong_points", 0) >= 3
                        else "LONG LOW FART. You still disagree but accept it."
                        if s.extra.get("strong_points", 0) >= 1
                        else "SHORT LOUD FART. You concede."
                    ),
                    strong_points=s.extra.get("strong_points", 0),
                ),
                affects_phase=True,
            ),
            Trigger(
                name="user_repeat",
                check=lambda _s, r: _detect_repeat(r),
                prompt=lambda _s, ps: ps.get("debate_bicker_repeat"),
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
