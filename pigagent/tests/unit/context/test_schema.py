# tests/unit/test_context_schema.py
"""Unit tests for context schemas  -  ConversationRecord, SummaryRecord, TokenBudget,
UserMemory, RoastContext, WorkingContext."""

import json

import pytest

from core.llm.types import Message, ToolCall
from context.schema import (
    WorkingContext, UserMemory, TokenBudget, RoastContext, ConversationRecord,
)
from agent_config import get_config

_cfg = get_config()


# -------------------------------------------------------------------------------
# TokenBudget
# -------------------------------------------------------------------------------

class TestTokenBudget:
    def test_defaults(self):
        b = TokenBudget()
        assert b.total_cap == _cfg.CONTEXT_TOKEN_BUDGET_CAP
        assert b.used == 0
        assert b.remaining == _cfg.CONTEXT_TOKEN_BUDGET_CAP

    def test_used_calculation(self):
        b = TokenBudget(
            layer_1_system=1000,
            layer_2_user_pref=500,
            layer_3_session=3000,
            layer_4_roast_prompt=2000,
            layer_4_roast_turns=1500,
        )
        assert b.used == 8000
        assert b.remaining == _cfg.CONTEXT_TOKEN_BUDGET_CAP - 8000

    def test_to_dict(self):
        b = TokenBudget(layer_1_system=100)
        d = b.to_dict()
        assert d["layer_1_system"] == 100
        assert "remaining" in d


# -------------------------------------------------------------------------------
# RoastContext
# -------------------------------------------------------------------------------

class TestRoastContext:
    def test_defaults(self):
        rc = RoastContext(roast_instance_id="r1")
        assert rc.roast_instance_id == "r1"
        assert rc.prompt == ""
        assert rc.turns == []
        assert rc.summary == ""

    def test_is_active(self):
        assert RoastContext(roast_instance_id="r1").is_active
        assert not RoastContext(roast_instance_id="").is_active

    def test_total_tokens(self):
        rc = RoastContext(roast_instance_id="r1", prompt_tokens=100, turns_tokens=200, summary_tokens=50)
        assert rc.total_tokens == 350

    def test_to_meta(self):
        rc = RoastContext(roast_instance_id="r1")
        rc.turns = [Message.user("hi")]
        meta = rc.to_meta()
        assert meta["roast_instance_id"] == "r1"
        assert meta["turn_count"] == 1


# -------------------------------------------------------------------------------
# WorkingContext
# -------------------------------------------------------------------------------

class TestWorkingContext:
    def test_defaults(self):
        wc = WorkingContext(user_id="u1")
        assert wc.summary == ""
        assert wc.summary_end_turn == 0
        assert wc.raw_records == []
        assert wc.roast is None
        assert wc.user_memory is None

    def test_to_messages_empty(self):
        wc = WorkingContext(user_id="u1")
        msgs = wc.to_messages()
        assert msgs == []

    def test_to_messages_bare_context(self):
        wc = WorkingContext(user_id="u1")
        msgs = wc.to_messages()
        assert msgs == []

    def test_to_messages_with_user_memory(self):
        wc = WorkingContext(
            user_id="u1",
            user_memory=UserMemory(user_id="u1", profile_summary="User likes sports."),
        )
        msgs = wc.to_messages()
        assert len(msgs) == 1
        assert "User likes sports" in msgs[0].content

    def test_to_messages_with_summary(self):
        wc = WorkingContext(
            user_id="u1",
            summary="Talked about weather and food.",
        )
        msgs = wc.to_messages()
        assert len(msgs) == 1  # L3 summary only

    def test_to_messages_with_raw_records(self):
        wc = WorkingContext(
            user_id="u1",
            raw_records=[
                ConversationRecord(turn_number=1, role="user", content="hello", created_at=100.0),
                ConversationRecord(turn_number=2, role="assistant", content="hi", created_at=101.0),
            ],
        )
        msgs = wc.to_messages()
        assert len(msgs) == 2  # 2 turns (oldest->newest)
        assert msgs[0].role == "user"

    def test_to_messages_with_roast(self):
        wc = WorkingContext(
            user_id="u1",
            roast=RoastContext(
                roast_instance_id="r1",
                summary="Game: trivia challenge\n\n---\n\nEarlier: user answered 3 questions.",
            ),
            raw_records=[
                ConversationRecord(turn_number=1, role="user", content="answer D", created_at=100.0),
                ConversationRecord(turn_number=2, role="assistant", content="correct!", created_at=101.0),
            ],
        )
        msgs = wc.to_messages()
        assert len(msgs) == 3
        assert "trivia challenge" in msgs[0].content
        assert msgs[0].role == "user"

    def test_budget_summary(self):
        wc = WorkingContext(user_id="u1")
        wc.budget.layer_1_system = 100
        summary = wc.budget_summary()
        assert summary["breakdown"]["L1_system"] == 100


class TestWorkingContextRawTurns:
    """WorkingContext.to_messages with ConversationRecord in raw_records."""

    def test_to_messages_with_conversation_records(self):
        from context.schema import WorkingContext, ConversationRecord
        cr = ConversationRecord(turn_number=1, role="user", content="hello", created_at=100.0)
        wc = WorkingContext(user_id="u1", raw_records=[cr])
        msgs = wc.to_messages()
        assert len(msgs) == 1
        assert msgs[0].role == "user"
        assert msgs[0].content == "hello"

    def test_to_messages_with_roast_and_no_summary(self):
        from context.schema import WorkingContext, RoastContext
        wc = WorkingContext(
            user_id="u1",
            roast=RoastContext(roast_instance_id="r1", summary=""),
        )
        msgs = wc.to_messages()
        assert len(msgs) == 0

    def test_budget_summary_full(self):
        from context.schema import WorkingContext
        wc = WorkingContext(user_id="u1")
        wc.budget.layer_1_system = 1000
        wc.budget.layer_2_user_pref = 500
        wc.budget.layer_3_session = 3000
        wc.budget.layer_4_roast_prompt = 2000
        s = wc.budget_summary()
        assert s["total_cap"] == 200_000
        assert s["used"] == 6500
        assert s["remaining"] == 193_500
        assert s["breakdown"]["L2_user_pref"] == 500
        assert s["breakdown"]["L3_session"] == 3000
        assert s["breakdown"]["L4_roast_prompt"] == 2000


# -------------------------------------------------------------------------------
# UserMemory
# -------------------------------------------------------------------------------

class TestUserMemory:
    def test_defaults(self):
        um = UserMemory(user_id="u1")
        assert um.profile_summary == ""

    def test_to_hash_and_back(self):
        um = UserMemory(
            user_id="u1",
            profile_summary="User likes sports.",
            stats={"total_turns": 10},
        )
        h = um.to_hash()
        restored = UserMemory.from_hash(h)
        assert restored.profile_summary == "User likes sports."

    def test_empty_stats(self):
        um = UserMemory(user_id="u1")
        h = um.to_hash()
        restored = UserMemory.from_hash(h)
        assert restored.stats == {}

    def test_token_count(self):
        um = UserMemory(user_id="u1", profile_summary="hello")
        assert um.token_count() > 0


# -------------------------------------------------------------------------------
# ConversationRecord
# -------------------------------------------------------------------------------

class TestConversationRecord:
    """ConversationRecord  -  serialization and conversion to Message."""

    def test_to_message_basic(self):
        from context.schema import ConversationRecord
        cr = ConversationRecord(turn_number=1, role="user", content="hello", created_at=100.0)
        msg = cr.to_message()
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.tool_calls is None
        assert msg.partial is False

    def test_to_message_partial(self):
        from context.schema import ConversationRecord
        cr = ConversationRecord(turn_number=2, role="assistant", content="cont...",
                                 created_at=101.0, partial=True)
        msg = cr.to_message()
        assert msg.partial is True

    def test_to_message_with_tool_calls_dicts(self):
        from context.schema import ConversationRecord
        tcs = [{"id": "c1", "name": "search", "arguments": '{"q":"x"}'}]
        cr = ConversationRecord(turn_number=3, role="assistant", content="",
                                 created_at=102.0, tool_calls=tcs)
        msg = cr.to_message()
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].id == "c1"
        assert msg.tool_calls[0].name == "search"

    def test_to_message_with_tool_call_objects(self):
        from context.schema import ConversationRecord
        from core.llm.types import ToolCall
        tc = ToolCall(id="c2", name="calc", arguments="{}")
        cr = ConversationRecord(turn_number=4, role="assistant", content="",
                                 created_at=103.0, tool_calls=[tc])
        msg = cr.to_message()
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].id == "c2"

    def test_to_message_with_tool_and_name(self):
        from context.schema import ConversationRecord
        cr = ConversationRecord(turn_number=5, role="tool", content="result",
                                 created_at=104.0, tool_call_id="c1", name="search")
        msg = cr.to_message()
        assert msg.tool_call_id == "c1"
        assert msg.name == "search"

    def test_to_dict_basic(self):
        from context.schema import ConversationRecord
        cr = ConversationRecord(turn_number=1, role="user", content="hi", created_at=100.0)
        d = cr.to_dict()
        assert d["turn"] == 1
        assert d["role"] == "user"
        assert d["content"] == "hi"
        assert "roast_instance_id" not in d
        assert "ts" in d

    def test_to_dict_with_all_fields(self):
        from context.schema import ConversationRecord
        tcs = [{"id": "c1", "name": "s", "arguments": "{}"}]
        cr = ConversationRecord(
            turn_number=10, role="assistant", content="ok", created_at=200.0,
            roast_instance_id="r1", tool_calls=tcs, tool_call_id="c1",
            name="s", partial=True,
        )
        d = cr.to_dict()
        assert d["roast_instance_id"] == "r1"
        assert d["partial"] is True
        assert d["name"] == "s"
        assert "tool_calls" in d

    def test_from_dict_basic(self):
        from context.schema import ConversationRecord
        d = {"turn": 5, "role": "assistant", "content": "reply", "ts": 300.0}
        cr = ConversationRecord.from_dict(d)
        assert cr.turn_number == 5
        assert cr.role == "assistant"
        assert cr.content == "reply"
        assert cr.roast_instance_id is None
        assert cr.partial is False

    def test_from_dict_with_roast_instance_id(self):
        from context.schema import ConversationRecord
        d = {"turn": 8, "role": "user", "content": "play", "roast_instance_id": "rx", "ts": 400.0}
        cr = ConversationRecord.from_dict(d)
        assert cr.roast_instance_id == "rx"

    def test_from_dict_tool_calls_json_string(self):
        from context.schema import ConversationRecord
        import json
        tcs = [{"id": "c1", "name": "f", "arguments": "{}"}]
        d = {"turn": 9, "role": "assistant", "content": "",
             "tool_calls": json.dumps(tcs), "ts": 500.0}
        cr = ConversationRecord.from_dict(d)
        assert cr.tool_calls == tcs

    def test_from_dict_tool_calls_already_list(self):
        from context.schema import ConversationRecord
        tcs = [{"id": "c1", "name": "f", "arguments": "{}"}]
        d = {"turn": 9, "role": "assistant", "content": "",
             "tool_calls": tcs, "ts": 500.0}
        cr = ConversationRecord.from_dict(d)
        assert cr.tool_calls == tcs

    def test_roundtrip(self):
        from context.schema import ConversationRecord
        cr = ConversationRecord(
            turn_number=42, role="assistant", content="done", created_at=999.0,
            roast_instance_id="rx", tool_calls=[{"id": "c1", "name": "f", "arguments": "{}"}],
            tool_call_id="c1", name="f", partial=True,
        )
        restored = ConversationRecord.from_dict(cr.to_dict())
        assert restored.turn_number == 42
        assert restored.role == "assistant"
        assert restored.roast_instance_id == "rx"
        assert restored.partial is True


# -------------------------------------------------------------------------------
# SummaryRecord
# -------------------------------------------------------------------------------

class TestSummaryRecord:
    """SummaryRecord  -  serialize/deserialize with end_turn anchor."""

    def test_serialize(self):
        from context.schema import SummaryRecord
        sr = SummaryRecord(text="compressed summary", end_turn=50)
        raw = sr.serialize()
        import json
        data = json.loads(raw)
        assert data["text"] == "compressed summary"
        assert data["end_turn"] == 50

    def test_deserialize(self):
        from context.schema import SummaryRecord
        import json
        raw = json.dumps({"text": "summary", "end_turn": 30})
        sr = SummaryRecord.deserialize(raw)
        assert sr.text == "summary"
        assert sr.end_turn == 30

    def test_deserialize_invalid_json_falls_back_to_raw(self):
        from context.schema import SummaryRecord
        sr = SummaryRecord.deserialize("plain text, not json")
        assert sr.text == "plain text, not json"
        assert sr.end_turn == 0

    def test_deserialize_missing_keys(self):
        from context.schema import SummaryRecord
        import json
        raw = json.dumps({"text": "only text"})
        sr = SummaryRecord.deserialize(raw)
        assert sr.text == "only text"
        assert sr.end_turn == 0

    def test_roundtrip(self):
        from context.schema import SummaryRecord
        sr = SummaryRecord(text="recursive summary", end_turn=99)
        restored = SummaryRecord.deserialize(sr.serialize())
        assert restored.text == "recursive summary"
        assert restored.end_turn == 99

    def test_default_end_turn(self):
        from context.schema import SummaryRecord
        sr = SummaryRecord(text="no end turn")
        assert sr.end_turn == 0
