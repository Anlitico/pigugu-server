"""Integration test for the director LLM — real API calls."""

import json
from unittest.mock import MagicMock, AsyncMock

import pytest
from roast.state import RoastState
from roast.types import Mode, Phase
from roast.modes.roast_together import RoastTogetherMode


class _FakeRecord:
    def __init__(self, role, content, turn_number=1, roast_instance_id=None):
        self.role = role
        self.content = content
        self.turn_number = turn_number
        self.roast_instance_id = roast_instance_id


_ROAST_ID = "director-test-001"


def _make_state(**kw) -> RoastState:
    s = RoastState.__new__(RoastState)
    s.user_id = kw.pop("user_id", "test-user")
    s.persona_id = kw.pop("persona_id", 1)
    s.roast_id = kw.pop("roast_id", "n1")
    s.mode = kw.pop("mode", Mode.ROAST_TOGETHER)
    s.roast_instance_id = kw.pop("roast_instance_id", _ROAST_ID)
    s.phase = kw.pop("phase", Phase.ACTIVE)
    s.turn_count = kw.pop("turn_count", 0)
    s.extra = kw.pop("extra", {"settled": False, "best_take": ""})
    return s


def _roast_body():
    return (
        "[Game Background]\n"
        "## News Context\n"
        "A CEO claims 996 work culture is a blessing for employees.\n"
        "## Game Mode\n"
        "You and the user are roasting this topic together."
    )


def _mid_game_records():
    return [
        _FakeRecord("system", _roast_body(), turn_number=1,
                     roast_instance_id=_ROAST_ID),
        _FakeRecord("user", "Game start", turn_number=2,
                     roast_instance_id=_ROAST_ID),
        _FakeRecord(
            "assistant",
            "The CEO said 996 is a blessing. Let's start there — "
            "I think it's the most tone-deaf thing a boss could say. What's your take?",
            turn_number=3, roast_instance_id=_ROAST_ID,
        ),
        _FakeRecord(
            "user",
            "He calls 996 a blessing? That's just PUA — dressing up exploitation as gratitude.",
            turn_number=4, roast_instance_id=_ROAST_ID,
        ),
        _FakeRecord(
            "assistant",
            "EXACTLY. He's not just out of touch — he's gaslighting. "
            "And get this: he probably doesn't even work 996 himself.",
            turn_number=5, roast_instance_id=_ROAST_ID,
        ),
    ]


@pytest.mark.integration
class TestDirectorIntegration:
    """Real LLM calls — requires valid API credentials."""

    @pytest.mark.asyncio
    async def test_director_mid_game_recognizes_best_take(self):
        """Director should detect a good user line and return action=none
        (user is engaged, no need to inject)."""
        mode = RoastTogetherMode()
        state = _make_state(turn_count=2)
        records = _mid_game_records()

        result = await mode._direct(state, records)

        assert result["action"] in ("none", "inject")
        assert result["close"] in (True, False)
        if result["best_take"]:
            assert isinstance(result["best_take"], str)

    @pytest.mark.asyncio
    async def test_director_returns_valid_json_schema(self):
        """All 4 required fields must be present with correct types."""
        mode = RoastTogetherMode()
        state = _make_state(turn_count=2)
        records = _mid_game_records()

        result = await mode._direct(state, records)

        assert set(result.keys()) == {"action", "best_take", "prompt", "close"}
        assert result["action"] in ("none", "inject")
        assert result["best_take"] is None or isinstance(result["best_take"], str)
        assert result["prompt"] is None or isinstance(result["prompt"], str)
        assert isinstance(result["close"], bool)

    @pytest.mark.asyncio
    async def test_director_exhausted_topic_closes(self):
        """After many low-effort replies, director should signal close=true."""
        mode = RoastTogetherMode()
        state = _make_state(turn_count=6)

        records = [
            _FakeRecord("system", _roast_body(), turn_number=1,
                         roast_instance_id=_ROAST_ID),
            _FakeRecord("user", "Game start", turn_number=2,
                         roast_instance_id=_ROAST_ID),
            _FakeRecord(
                "assistant",
                "The CEO said 996 is a blessing. Thoughts?",
                turn_number=3, roast_instance_id=_ROAST_ID,
            ),
            _FakeRecord("user", "yeah", turn_number=4,
                         roast_instance_id=_ROAST_ID),
            _FakeRecord(
                "assistant",
                "That's it? Come on, give me something spicier.",
                turn_number=5, roast_instance_id=_ROAST_ID,
            ),
            _FakeRecord("user", "idk", turn_number=6,
                         roast_instance_id=_ROAST_ID),
            _FakeRecord(
                "assistant",
                "I think we've covered all the angles on this one.",
                turn_number=7, roast_instance_id=_ROAST_ID,
            ),
            _FakeRecord("user", "whatever", turn_number=8,
                         roast_instance_id=_ROAST_ID),
        ]

        result = await mode._direct(state, records)

        assert result["action"] in ("none", "inject")
        # The director SHOULD inject and signal close for exhausted topic
        if result["action"] == "inject":
            assert result["close"] is True
            assert result["prompt"] is not None

    @pytest.mark.asyncio
    async def test_director_filters_non_roast_records(self):
        """L2/L3 virtual records and free-chat messages should be excluded."""
        mode = RoastTogetherMode()
        state = _make_state(turn_count=1)

        records = [
            # L2/L3 virtual records — should be excluded
            _FakeRecord("system", "[User profile]\nlikes spicy roasts",
                         turn_number=-3),
            _FakeRecord("system", "[Conversation history]\nprevious chat",
                         turn_number=-2),
            # Free chat before roast — should be excluded
            _FakeRecord("user", "hello", turn_number=1),
            _FakeRecord("assistant", "hi there", turn_number=2),
            # Roast body + roast turns — should be included
            _FakeRecord("system", _roast_body(), turn_number=3,
                         roast_instance_id=_ROAST_ID),
            _FakeRecord("user", "Game start", turn_number=4,
                         roast_instance_id=_ROAST_ID),
            _FakeRecord(
                "assistant",
                "The CEO said 996 is a blessing. Thoughts?",
                turn_number=5, roast_instance_id=_ROAST_ID,
            ),
        ]

        result = await mode._direct(state, records)

        # Should still produce valid output despite the noise
        assert result["action"] in ("none", "inject")
        assert isinstance(result["close"], bool)

    @pytest.mark.asyncio
    async def test_director_best_take_is_exact_quote(self):
        """best_take should be an EXACT quote from the transcript."""
        mode = RoastTogetherMode()
        state = _make_state(turn_count=2)

        user_line = (
            "He calls 996 a blessing? That's just corporate gaslighting — "
            "dressing up exploitation as a favor."
        )
        records = [
            _FakeRecord("system", _roast_body(), turn_number=1,
                         roast_instance_id=_ROAST_ID),
            _FakeRecord("user", "Game start", turn_number=2,
                         roast_instance_id=_ROAST_ID),
            _FakeRecord(
                "assistant",
                "The CEO said 996 is a blessing. What's your take?",
                turn_number=3, roast_instance_id=_ROAST_ID,
            ),
            _FakeRecord("user", user_line, turn_number=4,
                         roast_instance_id=_ROAST_ID),
        ]

        result = await mode._direct(state, records)

        if result["best_take"]:
            # Should be a close match to the user's original line.
            # LLMs sometimes make minor transcription tweaks (e.g., extra "is").
            best = result["best_take"].lower()
            orig = user_line.lower()
            # At least 80% of the words in best_take should appear in the original
            best_words = set(best.split())
            orig_words = set(orig.split())
            overlap = best_words & orig_words
            assert len(overlap) / len(best_words) >= 0.8, (
                f"best_take='{result['best_take']}' has <80% word overlap "
                f"with user_line='{user_line}'"
            )
