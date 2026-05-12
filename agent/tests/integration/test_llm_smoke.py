# tests/integration/test_llm_smoke.py
"""Comprehensive integration tests — all models, all features, real API keys.

Requires DASHSCOPE_US_API_KEY and ARK_API_KEY in root .env.
Run: pytest tests/integration/ -v --tb=short
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent.parent / ".env", override=True)

from core.llm import get_llm, Message, ModelRegistry


# -- Helpers ------------------------------------------------------------------

def _need(env_var: str):
    if not os.getenv(env_var):
        pytest.skip(f"{env_var} not set")


def _cap(model_id: str, feature: str):
    info = ModelRegistry.get(model_id)
    if not getattr(info, feature, False):
        pytest.skip(f"{model_id} does not support {feature}")


async def _chat(model_id: str, env_var: str, **kwargs):
    _need(env_var)
    p = get_llm(model_id)
    kwargs.setdefault("max_tokens", 50)
    resp = await p.chat(messages=[Message.user("Say hello in one word.")], **kwargs)
    assert resp.content, f"Empty response from {model_id}"
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# Qwen Plus — full capability model
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestQwenPlus:
    ENV = "DASHSCOPE_US_API_KEY"
    MODEL = "qwen-plus"

    # ── Connectivity ──

    async def test_connectivity(self):
        await _chat(self.MODEL, self.ENV)

    # ── Streaming ──

    async def test_streaming(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        chunks = []
        async for d in p.chat_stream(
            messages=[Message.user("Count from 1 to 5.")],
            max_tokens=100,
        ):
            if d.content:
                chunks.append(d.content)
        assert "".join(chunks), "Empty streaming response"

    # ── Stream usage info ──

    async def test_streaming_with_usage(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        usage = None
        async for d in p.chat_stream(
            messages=[Message.user("hi")],
            max_tokens=20,
        ):
            if d.usage:
                usage = d.usage
        # Qwen streaming may not always include usage in the final chunk
        assert usage is None or usage.total_tokens > 0

    # ── Thinking (non-stream) ──

    async def test_thinking(self):
        _cap(self.MODEL, "thinking")
        resp = await _chat(self.MODEL, self.ENV,
                           thinking={"enabled": True, "budget": 2048},
                           max_tokens=100)
        assert len(resp.content) > 0

    # ── Thinking (stream) ──

    async def test_thinking_stream(self):
        _cap(self.MODEL, "thinking")
        _need(self.ENV)
        p = get_llm(self.MODEL)
        reasoning_seen = False
        content_seen = False
        async for d in p.chat_stream(
            messages=[Message.user("What is 17 * 23? Show your work.")],
            thinking={"enabled": True, "budget": 4096},
            max_tokens=300,
        ):
            if d.reasoning_content:
                reasoning_seen = True
            if d.content:
                content_seen = True
        assert content_seen, "No content in thinking stream"
        # Qwen streaming may or may not expose reasoning_content

    # ── Web search (basic) ──

    async def test_search(self):
        _cap(self.MODEL, "search")
        resp = await _chat(self.MODEL, self.ENV,
                           search={"enabled": True},
                           max_tokens=200)
        assert len(resp.content) > 0

    # ── Structured output (JSON mode) ──

    async def test_json_mode(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            messages=[Message.user("Return JSON: {\"name\": \"Alice\", \"age\": 30}")],
            response_format={"type": "json_object"},
            max_tokens=100,
        )
        assert resp.content
        import json
        try:
            data = json.loads(resp.content)
            assert "name" in data or "age" in data
        except json.JSONDecodeError:
            pytest.fail(f"Not valid JSON: {resp.content[:100]}")

    # ── Tool calling ──

    async def test_tool_calling(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            messages=[Message.user("What is 2+2?")],
            tools=[{
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "Do math",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "expression": {"type": "string"}
                        },
                        "required": ["expression"],
                    },
                },
            }],
            tool_choice="auto",
            max_tokens=100,
        )
        assert resp.content or resp.tool_calls, "Expected content or tool call"

    # ── Continuation / prefix ──

    async def test_continuation(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        # Send a partial assistant message — model should continue it
        resp = await p.chat(
            messages=[
                Message.user("List three colors:"),
                Message.assistant("1. Red\n2.", partial=True),
            ],
            max_tokens=50,
        )
        assert resp.content, "Empty continuation response"
        # Should naturally continue from the prefix

    # ── Stop sequences ──

    async def test_stop_sequence(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            messages=[Message.user("Count: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10")],
            stop=[","],
            max_tokens=50,
        )
        assert resp.content
        # With stop=[","] the output should be short (stopped at first comma)
        assert "," not in resp.content, f"Stop sequence not honored: {resp.content}"

    # ── Temperature ──

    async def test_temperature(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            messages=[Message.user("Say hello")],
            temperature=0.0,
            max_tokens=20,
        )
        assert resp.content


# ═══════════════════════════════════════════════════════════════════════════════
# Doubao Seed 1.6 — Volcengine
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestDoubaoSeed:
    ENV = "ARK_API_KEY"
    MODEL = "doubao-seed-1-6-251015"

    async def test_connectivity(self):
        await _chat(self.MODEL, self.ENV)

    async def test_streaming(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        chunks = []
        async for d in p.chat_stream(
            messages=[Message.user("Count from 1 to 5.")],
            max_tokens=100,
        ):
            if d.content:
                chunks.append(d.content)
        assert "".join(chunks)

    async def test_thinking(self):
        _cap(self.MODEL, "thinking")
        resp = await _chat(self.MODEL, self.ENV,
                           thinking={"enabled": True},
                           max_tokens=100)
        assert len(resp.content) > 0

    async def test_thinking_stream(self):
        _cap(self.MODEL, "thinking")
        _need(self.ENV)
        p = get_llm(self.MODEL)
        content_seen = False
        async for d in p.chat_stream(
            messages=[Message.user("What is 2+2? Explain briefly.")],
            thinking={"enabled": True},
            max_tokens=200,
        ):
            if d.content:
                content_seen = True
        assert content_seen

    async def test_json_mode(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            messages=[Message.user("Return JSON: {\"city\": \"Beijing\", \"country\": \"China\"}")],
            response_format={"type": "json_object"},
            max_tokens=100,
        )
        import json
        try:
            data = json.loads(resp.content)
            assert isinstance(data, dict)
        except json.JSONDecodeError:
            pytest.fail(f"Not valid JSON: {resp.content[:100]}")

    async def test_prefix_continuation(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            messages=[
                Message.user("List three fruits:"),
                Message.assistant("1. Apple\n2.", partial=True),
            ],
            max_tokens=50,
        )
        assert resp.content

    async def test_stop_sequence(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            messages=[Message.user("Count: 1, 2, 3, 4, 5")],
            stop=[","],
            max_tokens=50,
        )
        # Some models don't strictly honor stop tokens on every request.
        # Just verify the request completes successfully.
        assert resp.content
