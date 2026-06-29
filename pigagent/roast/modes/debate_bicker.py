"""DebateBicker (Debate) — Pigugu picks a controversial side; the user argues back.

Director outputs per-round polling data (user_support / opponent_support /
shift / judge_comment), pushed to the App in real time via Redis Pub/Sub.

KO is triggered when support crosses the 75%/25% thresholds.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from loguru import logger

from roast.types import Mode, Phase
from roast.base import GameMode, Trigger
from roast import pending

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


def _debate_result(final_user_support: float) -> str:
    """Map final user support percentage to a debate result enum."""
    if final_user_support >= 75:
        return "landslide_win"
    elif final_user_support >= 55:
        return "narrow_win"
    elif final_user_support >= 45:
        return "draw"
    elif final_user_support >= 26:
        return "narrow_loss"
    else:
        return "landslide_loss"


class DebateBickerMode(GameMode):
    mode = Mode.DEBATE_BICKER
    max_turns = 8

    async def get_system_prompt_extension(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("debate_bicker_system")

    async def get_director_prompt(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("debate_bicker_director")

    def get_director_schema(self) -> dict:
        """Debate director output: base fields + polling/commentary fields."""
        return {
            "name": "director_output",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["none", "inject"]},
                    "best_take": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    },
                    "prompt": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    },
                    "close": {"type": "boolean"},
                    "user_support": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 100.0,
                        "description": "Current public support percentage for the user (0-100).",
                    },
                    "opponent_support": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 100.0,
                        "description": "Current support for Pigugu (= 100 - user_support).",
                    },
                    "shift": {
                        "type": "number",
                        "description": "Support change this round (positive = user gained, negative = Pigugu gained).",
                    },
                    "judge_comment": {
                        "type": "string",
                        "description": "Director's 1-2 sentence commentary on this round.",
                    },
                },
                "required": [
                    "action", "best_take", "prompt", "close",
                    "user_support", "opponent_support", "shift", "judge_comment",
                ],
                "additionalProperties": False,
            },
        }

    # ── State helpers ──────────────────────────────────────────────────

    @staticmethod
    def init_extra() -> dict:
        return {
            "strong_points": 0,
            "fart_type": "",
            "debate_history": [],
            "best_take": "",
            "support_history": [],
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

    # ── Real-time judging push ──────────────────────────────────────────

    async def _on_director_result(
        self, state: RoastState, director_result: dict, redis,
    ) -> None:
        """Publish debate_judge event and check KO thresholds."""
        if not redis:
            return

        user_support = director_result.get("user_support", 50.0)
        opponent_support = director_result.get("opponent_support", 50.0)
        shift = director_result.get("shift", 0.0)
        judge_comment = director_result.get("judge_comment", "")

        # Persist to state for later settlement
        support_history: list = state.extra.setdefault("support_history", [])
        support_history.append({
            "round": state.turn_count,
            "user": user_support,
            "opponent": opponent_support,
            "shift": shift,
        })

        # Build the debate_judge event (per TD §3.2.3)
        event = {
            "type": "debate_judge",
            "roast_instance_id": state.roast_instance_id,
            "roast_id": state.roast_id,
            "mode_id": "debate",
            "round": state.turn_count,
            "user_support": user_support,
            "opponent_support": opponent_support,
            "shift": shift,
            "judge_comment": judge_comment,
        }

        try:
            await redis.publish(
                f"ws:user:{state.user_id}",
                json.dumps(event),
            )
            logger.info(
                f"[{self.mode}] debate_judge pushed: round={state.turn_count} "
                f"user={user_support:.1f}% opponent={opponent_support:.1f}% "
                f"shift={shift:+.1f}%"
            )
        except Exception as e:
            logger.error(f"[{self.mode}] Failed to publish debate_judge: {e}")

        # ── KO detection ──────────────────────────────────────────────
        if user_support >= 75.0 or user_support <= 25.0:
            state.phase = Phase.CLOSING
            state.extra["ko"] = True
            closing_prompt = (
                "THE DEBATE IS OVER — public opinion has reached a decisive verdict. "
                "Wrap up with a closing statement acknowledging the result. "
                "Call mark_roast_complete when done."
            )
            try:
                await pending.write(state.roast_instance_id, closing_prompt, redis)
            except Exception as e:
                logger.error(f"[{self.mode}] Failed to write KO closing prompt: {e}")
            logger.info(
                f"[{self.mode}] KO triggered: user_support={user_support:.1f}% "
                f"roast={state.roast_instance_id} turn={state.turn_count}"
            )

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

        # Compute debate result from final support
        support_history: list = state.extra.get("support_history", [])
        final_support = 50.0
        if support_history:
            final_support = support_history[-1].get("user", 50.0)

        return {
            "mode": str(self.mode),
            "result": result,
            "strong_points": strong,
            "final_user_support": final_support,
            "debate_result": _debate_result(final_support),
            "support_history": support_history,
        }


def _detect_repeat(records: list) -> bool:
    user_turns = [r for r in records[-4:]
                  if getattr(r, "role", "") == "user"]
    if len(user_turns) >= 2:
        a = getattr(user_turns[-1], "content", "").strip().lower()
        b = getattr(user_turns[-2], "content", "").strip().lower()
        return bool(a and a == b)
    return False
