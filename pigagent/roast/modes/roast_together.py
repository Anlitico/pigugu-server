"""RoastTogether — roast a hot topic together with the user.

State (extra):
    user_energy   -  0.0-1.0, engagement level per turn
    best_take     -  the user's spiciest line so far
    best_take_turn  -  which turn it happened
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from roast.types import Mode
from roast.base import GameMode, Trigger
from roast.prompts import render

if TYPE_CHECKING:
    from roast.state import RoastState


def _compute_energy(text: str) -> float:
    """Heuristic energy score from a user message. 0.0 (flat)  -  1.0 (on fire).

    Six signals, each contributing a capped sub-score:

    | Signal           | Max  | Logic                                              |
    |------------------|------|----------------------------------------------------|
    | length           | 0.40 | char count / 200 (longer = more engaged)           |
    | exclamation      | 0.15 | 0.05 per '!' (excitement)                          |
    | CAPS             | 0.10 | 0.03 per ALL-CAPS substring >= 2 chars (emphasis)  |
    | multi-bang       | 0.10 | '!!!' '!!' '! !' patterns (overflowing emotion)     |
    | spicy words      | 0.10 | 'absolutely' 'insane' 'ridiculous' etc.             |
    | word count       | 0.10 | >= 15 words (verbal engagement)                     |

    Total capped at 1.0. Rule-based is fast and good enough for voice;
    a sampling LLM check every N turns could be added later for depth.
    """
    if not text:
        return 0.0
    score = 0.0
    score += min(len(text) / 200, 0.40)
    score += min(text.count("!") * 0.05, 0.15)
    score += min(len(re.findall(r"[A-Z]{2,}", text)) * 0.03, 0.10)
    score += 0.10 if re.search(r"!!!|! !|!!", text) else 0.0
    score += 0.10 if re.search(r"absolutely|totally|completely|insane|ridiculous|outrageous", text, re.I) else 0.0
    score += 0.10 if len(text.split()) >= 15 else 0.0
    return min(score, 1.0)


def _user_disengaged(state, records: list) -> bool:
    if state.turn_count < 3 or len(records) < 3:
        return False
    user_msgs = [r for r in records[-3:] if getattr(r, "role", "") == "user"]
    if len(user_msgs) < 2:
        return False
    avg_len = sum(len(getattr(r, "content", "")) for r in user_msgs) / len(user_msgs)
    return avg_len < 20


def _saturated(state, records: list) -> bool:
    """Topic exhausted: enough turns have passed AND user energy has been
    consistently low, indicating the conversation naturally ran its course."""
    if state.turn_count < 3 or len(records) < 3:
        return False
    user_msgs = [r for r in records[-3:] if getattr(r, "role", "") == "user"]
    if len(user_msgs) < 2:
        return False
    energies = [_compute_energy(getattr(r, "content", "")) for r in user_msgs]
    # Last 2+ user turns all below 0.50 energy
    return all(e < 0.50 for e in energies)


class RoastTogetherMode(GameMode):
    mode = Mode.ROAST_TOGETHER
    max_turns = 8

    @property
    def system_prompt_extension(self) -> str:
        return render("roast_together_system")

    # ── State helpers ──────────────────────────────────────────────────

    # ── Quotable threshold ─────────────────────────────────────────────
    # A line must pass ALL three filters to be considered quotable:
    #   1. length >= 15 chars (not just "yeah" or "lol")
    #   2. energy >= 0.50 (genuinely engaged, not flat)
    #   3. energy beats the current best by enough margin (> 0.05)
    # This avoids extracting boring lines that would look bad on the App.

    _QUOTABLE_MIN_LENGTH = 15
    _QUOTABLE_MIN_ENERGY = 0.50
    _QUOTABLE_MARGIN = 0.05

    @staticmethod
    def init_extra() -> dict:
        return {
            "user_energy": 0.0,
            "best_take": "",
            "best_take_energy": 0.0,
            "best_take_turn": 0,
            "has_best_take": False,
            "score_breakdown": {},
            "settled": False,
        }

    # ── Advance ────────────────────────────────────────────────────────

    async def tick(
        self,
        state: RoastState,
        *,
        records: list,
        redis,
        pg_pool=None,
    ) -> str | None:
        """Update mode-specific state, then run base trigger checks."""
        self._update_state(state, records)
        return await super().tick(state, records=records, redis=redis, pg_pool=pg_pool)

    def _update_state(self, state: RoastState, records: list) -> None:
        """Compute user_energy and track best_take from recent user turn."""
        user_turns = [r for r in records
                      if getattr(r, "role", "") == "user"]
        if not user_turns:
            return

        latest = user_turns[-1]
        text = getattr(latest, "content", "")

        energy = _compute_energy(text)
        state.extra["user_energy"] = energy

        # Track best quote  -  only if genuinely quotable
        if (
            len(text) >= self._QUOTABLE_MIN_LENGTH
            and energy >= self._QUOTABLE_MIN_ENERGY
            and energy > state.extra.get("best_take_energy", 0) + self._QUOTABLE_MARGIN
        ):
            state.extra["best_take"] = text
            state.extra["best_take_energy"] = energy
            state.extra["best_take_turn"] = state.turn_count

    # ── Triggers ───────────────────────────────────────────────────────

    @property
    def triggers(self) -> list[Trigger]:
        return [
            Trigger(
                name="roast_saturated",
                check=lambda s, r: _saturated(s, r),
                prompt=lambda s: render(
                    "roast_together_ending",
                    best_take=s.extra.get("best_take", ""),
                ),
                affects_phase=True,
            ),
            Trigger(
                name="ending_max_turns",
                check=lambda s, r: s.turn_count >= self.max_turns,
                prompt=lambda s: render(
                    "roast_together_ending",
                    best_take=s.extra.get("best_take", ""),
                ),
                affects_phase=True,
            ),
            Trigger(
                name="user_spicy",
                check=lambda s, r: (
                    s.turn_count >= 2
                    and s.extra.get("user_energy", 0) > 0.7
                ),
                prompt=render("roast_together_spicy"),
            ),
            Trigger(
                name="user_disengaged",
                check=lambda s, r: _user_disengaged(s, r),
                prompt=render("roast_together_disengaged"),
            ),
        ]

    def score(self, state: RoastState) -> dict:
        return {
            "mode": str(self.mode),
            "turns": state.turn_count,
            "best_take": state.extra.get("best_take", ""),
        }
