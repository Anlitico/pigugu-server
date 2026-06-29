"""RoastTogether (Hot Take) — roast a hot topic together with the user.

Director outputs per-round scoring (score / rating / highlighted_quote),
which is pushed to the App in real time via Redis Pub/Sub.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from loguru import logger

from roast.types import Mode
from roast.base import GameMode, Trigger

if TYPE_CHECKING:
    from roast.state import RoastState
    from prompts import PromptStore


class RoastTogetherMode(GameMode):
    mode = Mode.ROAST_TOGETHER
    max_turns = 8

    async def get_system_prompt_extension(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("roast_together_system")

    async def get_director_prompt(self, prompt_store: PromptStore | None) -> str:
        if prompt_store is None:
            return ""
        return await prompt_store.get("roast_together_director")

    def get_director_schema(self) -> dict:
        """Hot Take director output: base fields + score/rating/highlighted_quote."""
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
                    "score": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 12,
                        "description": "Score for this turn (1-12). 11=Superb, 12=Godlike.",
                    },
                    "rating": {
                        "type": "string",
                        "enum": ["meh", "decent", "spicy", "fire", "superb", "godlike"],
                        "description": "Rating tier for this turn.",
                    },
                    "highlighted_quote": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ],
                        "description": "Exact quote from the user when rating is fire/superb/godlike (score >= 9), otherwise null.",
                    },
                },
                "required": [
                    "action", "best_take", "prompt", "close",
                    "score", "rating", "highlighted_quote",
                ],
                "additionalProperties": False,
            },
        }

    @staticmethod
    def init_extra() -> dict:
        return {
            "settled": False,
            "best_take": "",
            "scores": [],
        }

    # ── Real-time scoring push ───────────────────────────────────────────

    async def _on_director_result(
        self, state: RoastState, director_result: dict, redis,
    ) -> None:
        """Publish roast_score event to the App via Redis."""
        if not redis:
            return

        score = director_result.get("score", 0)
        rating = director_result.get("rating", "meh")
        highlighted_quote = director_result.get("highlighted_quote")

        # Persist to state for later settlement
        scores: list = state.extra.setdefault("scores", [])
        scores.append({
            "round": state.turn_count,
            "score": score,
            "rating": rating,
            "quote": highlighted_quote,
        })

        # Build the roast_score event (per TD §3.2.2)
        event = {
            "type": "roast_score",
            "roast_instance_id": state.roast_instance_id,
            "roast_id": state.roast_id,
            "mode_id": "poison_opinion",
            "round": state.turn_count,
            "score": score,
            "rating": rating,
            "highlighted_quote": highlighted_quote,
        }

        try:
            await redis.publish(
                f"ws:user:{state.user_id}",
                json.dumps(event),
            )
            logger.info(
                f"[{self.mode}] roast_score pushed: round={state.turn_count} "
                f"score={score} rating={rating}"
            )
        except Exception as e:
            logger.error(f"[{self.mode}] Failed to publish roast_score: {e}")

    # ── Triggers ───────────────────────────────────────────────────────

    @property
    def triggers(self) -> list[Trigger]:
        return [
            Trigger(
                name="ending_max_turns",
                check=lambda s, r: s.turn_count >= self.max_turns,
                prompt=lambda s, ps: ps.render(
                    "roast_together_ending",
                    best_take=s.extra.get("best_take", ""),
                ),
                affects_phase=True,
            ),
        ]

    def score(self, state: RoastState) -> dict:
        scores: list = state.extra.get("scores", [])
        total = sum(s.get("score", 0) for s in scores)
        count = len(scores)
        avg = round(total / count, 2) if count > 0 else 0.0

        # Find best rating (use dict lookup instead of .index() to avoid
        # ValueError on unrecognised ratings from LLM output).
        _RATING_RANK = {"meh": 0, "decent": 1, "spicy": 2, "fire": 3, "superb": 4, "godlike": 5}
        best_rating = "meh"
        best_quote = ""
        for s in scores:
            r = s.get("rating", "meh")
            if _RATING_RANK.get(r, -1) >= _RATING_RANK.get(best_rating, -1):
                best_rating = r
                best_quote = s.get("quote") or ""

        return {
            "mode": str(self.mode),
            "turns": state.turn_count,
            "settled": state.extra.get("settled", False),
            "best_take": state.extra.get("best_take", ""),
            "total_score": total,
            "avg_score": avg,
            "best_rating": best_rating,
            "best_quote": best_quote,
            "scores": scores,
        }
