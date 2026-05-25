# tests/unit/lk/test_bridge.py
"""Unit tests for PigAgentVoiceBridge."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from lk.bridge import PigAgentVoiceBridge


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_bridge(**kwargs):
    pig = MagicMock()
    pig.generate_reply = _spy_async_gen(["Hello!"])
    pig.stream = AsyncMock()
    pig.get_active_roast = AsyncMock(return_value=None)

    defaults = {
        "pig_agent": pig,
        "persona_id": "trump",
        "user_id": "u1",
    }
    defaults.update(kwargs)
    return PigAgentVoiceBridge(**defaults), pig


class CallTracker:
    """Wraps an async generator to track call args like a mock."""
    def __init__(self, chunks):
        self._chunks = chunks
        self.call_args = None
        self.call_args_list = []
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_args = (args, kwargs)
        self.call_args_list.append((args, kwargs))
        self.call_count += 1
        return self._gen()

    def assert_called_once(self):
        assert self.call_count == 1, f"Expected 1 call, got {self.call_count}"

    async def _gen(self):
        for c in self._chunks:
            yield c


def _spy_async_gen(chunks):
    return CallTracker(chunks)


def _make_chat_ctx(role_content_pairs):
    """Build a mock ChatContext with given items."""
    items = []
    for role, content in role_content_pairs:
        item = MagicMock()
        item.role = role
        item.text_content = content
        items.append(item)
    ctx = MagicMock()
    ctx.items = items
    return ctx


# ── _extract_user_text ──────────────────────────────────────────────────────


class TestExtractUserText:
    def test_returns_last_user_text(self):
        bridge, pig = _make_bridge()
        ctx = _make_chat_ctx([
            ("system", "sys prompt"),
            ("user", "hello"),
            ("assistant", "hi there"),
            ("user", "what's up"),
        ])
        assert bridge._extract_user_text(ctx) == "what's up"

    def test_returns_empty_when_no_user_message(self):
        bridge, pig = _make_bridge()
        ctx = _make_chat_ctx([
            ("system", "sys prompt"),
            ("assistant", "hello"),
        ])
        assert bridge._extract_user_text(ctx) == ""

    def test_returns_empty_when_empty_chat(self):
        bridge, pig = _make_bridge()
        ctx = _make_chat_ctx([])
        assert bridge._extract_user_text(ctx) == ""

    def test_skips_user_without_text_content(self):
        bridge, pig = _make_bridge()
        items = []
        item = MagicMock()
        item.role = "user"
        item.text_content = None
        items.append(item)
        item2 = MagicMock()
        item2.role = "user"
        item2.text_content = "valid input"
        items.append(item2)
        ctx = MagicMock()
        ctx.items = items
        assert bridge._extract_user_text(ctx) == "valid input"


# ── llm_node ────────────────────────────────────────────────────────────────


class TestLlmNode:
    def test_delegates_to_generate_reply(self):
        bridge, pig = _make_bridge()
        ctx = _make_chat_ctx([("user", "hello")])

        import asyncio
        result = asyncio.run(_run_collect(bridge.llm_node(ctx, [], MagicMock())))

        pig.generate_reply.assert_called_once()
        args, kwargs = pig.generate_reply.call_args
        assert args == ("u1", "hello")
        assert kwargs["persona_id"] == "trump"
        assert result == "Hello!"

    def test_empty_user_text_yields_nothing(self):
        bridge, pig = _make_bridge()
        pig.generate_reply = _spy_async_gen([])

        ctx = _make_chat_ctx([("user", "")])

        import asyncio
        result = asyncio.run(_run_collect(bridge.llm_node(ctx, [], MagicMock())))
        assert result == ""

    def test_passes_persona_id(self):
        bridge, pig = _make_bridge(persona_id="musk")
        ctx = _make_chat_ctx([("user", "hello")])

        import asyncio
        asyncio.run(_run_collect(bridge.llm_node(ctx, [], MagicMock())))

        _, kwargs = pig.generate_reply.call_args
        assert kwargs["persona_id"] == "musk"


# ── Properties ──────────────────────────────────────────────────────────────


class TestBridgeProperties:
    def test_allow_interruptions(self):
        bridge, _ = _make_bridge(allow_interruptions=False)
        assert not bridge.allow_interruptions

    def test_default_allow_interruptions(self):
        bridge, _ = _make_bridge()
        assert bridge.allow_interruptions

    def test_not_given_properties_return_not_given(self):
        from livekit.agents.types import NOT_GIVEN
        bridge, _ = _make_bridge()
        assert bridge.turn_detection is NOT_GIVEN
        assert bridge.mcp_servers is NOT_GIVEN
        assert bridge.min_consecutive_speech_delay is NOT_GIVEN
        assert bridge.use_tts_aligned_transcript is NOT_GIVEN

    def test_stt_node_is_none(self):
        bridge, _ = _make_bridge()
        assert bridge.stt_node is None

    def test_instructions_is_empty(self):
        bridge, _ = _make_bridge()
        assert bridge.instructions == ""

    def test_tools_is_empty(self):
        bridge, _ = _make_bridge()
        assert bridge.tools == []

    def test_on_exit_does_not_raise(self):
        bridge, _ = _make_bridge()
        import asyncio
        asyncio.run(bridge.on_exit())

    def test_on_user_turn_completed_is_noop(self):
        bridge, _ = _make_bridge()
        import asyncio
        asyncio.run(bridge.on_user_turn_completed(MagicMock(), MagicMock()))


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _run_collect(gen):
    chunks = []
    async for t in gen:
        chunks.append(str(t))
    return "".join(chunks)
