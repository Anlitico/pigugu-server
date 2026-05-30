# tests/integration/test_llm_smoke.py
"""Integration tests  -  all models, all features, real API keys.

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
    resp = await p.chat(messages=[Message.user("Say hello in one word.")], model=model_id, **kwargs)
    assert resp.content, f"Empty response from {model_id}"
    return resp


# -------------------------------------------------------------------------------
# Qwen Plus (legacy)
# -------------------------------------------------------------------------------

@pytest.mark.asyncio
class TestQwenPlus:
    ENV = "DASHSCOPE_US_API_KEY"
    MODEL = "qwen-plus-us"

    async def test_connectivity(self):
        await _chat(self.MODEL, self.ENV)

    async def test_streaming(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        chunks = []
        async for d in p.chat_stream(  # type: ignore[reportGeneralTypeIssues]
            model=self.MODEL,
            messages=[Message.user("Count from 1 to 5.")],
            max_tokens=100,
        ):
            if d.content:
                chunks.append(d.content)
        assert "".join(chunks), "Empty streaming response"

    async def test_thinking(self):
        _cap(self.MODEL, "thinking")
        resp = await _chat(self.MODEL, self.ENV,
                           thinking={"enabled": True, "budget": 2048},
                           max_tokens=100)
        assert len(resp.content) > 0

    async def test_json_mode(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            model=self.MODEL,
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

    async def test_tool_calling(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            model=self.MODEL,
            messages=[Message.user("What is 2+2?")],
            tools=[{
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "Do math",
                    "parameters": {
                        "type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"],
                    },
                },
            }],
            max_tokens=100,
        )
        assert resp.content or resp.tool_calls, "Expected content or tool call"

    async def test_search(self):
        _cap(self.MODEL, "search")
        resp = await _chat(self.MODEL, self.ENV,
                           search={"enabled": True},
                           max_tokens=200)
        assert len(resp.content) > 0

    async def test_temperature(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            model=self.MODEL,
            messages=[Message.user("Say hello")],
            temperature=0.0,
            max_tokens=20,
        )
        assert resp.content

    async def test_stop_sequence(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            model=self.MODEL,
            messages=[Message.user("Count: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10")],
            stop=[","],
            max_tokens=50,
        )
        assert resp.content


# -------------------------------------------------------------------------------
# Qwen Flash (US)
# -------------------------------------------------------------------------------

@pytest.mark.asyncio
class TestQwenFlash:
    ENV = "DASHSCOPE_US_API_KEY"
    MODEL = "qwen-flash-us"

    async def test_connectivity(self):
        await _chat(self.MODEL, self.ENV)

    async def test_streaming(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        chunks = []
        async for d in p.chat_stream(  # type: ignore[reportGeneralTypeIssues]
            model=self.MODEL,
            messages=[Message.user("Count from 1 to 5.")],
            max_tokens=100,
        ):
            if d.content:
                chunks.append(d.content)
        assert "".join(chunks), "Empty streaming response"

    async def test_json_mode(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            model=self.MODEL,
            messages=[Message.user("Return JSON: {\"city\": \"Shanghai\"}")],
            response_format={"type": "json_object"},
            max_tokens=100,
        )
        import json
        try:
            json.loads(resp.content)
        except json.JSONDecodeError:
            pytest.fail(f"Not valid JSON: {resp.content[:100]}")

    async def test_search(self):
        _cap(self.MODEL, "search")
        resp = await _chat(self.MODEL, self.ENV,
                           search={"enabled": True},
                           max_tokens=200)
        assert len(resp.content) > 0


# -------------------------------------------------------------------------------
# Doubao Seed 1.6  -  Volcengine
# -------------------------------------------------------------------------------

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
        async for d in p.chat_stream(  # type: ignore[reportGeneralTypeIssues]
            model=self.MODEL,
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

    async def test_json_mode(self):
        _need(self.ENV)
        p = get_llm(self.MODEL)
        resp = await p.chat(
            model=self.MODEL,
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


# -------------------------------------------------------------------------------
# Cross-model  -  model switching via the model= parameter
# -------------------------------------------------------------------------------

@pytest.mark.asyncio
class TestModelSwitching:
    """Same provider instance, different models via the model= parameter."""

    ENV = "DASHSCOPE_US_API_KEY"

    async def test_same_provider_different_models(self):
        _need(self.ENV)
        p = get_llm("qwen-flash-us")

        r1 = await p.chat(
            model="qwen-flash-us",
            messages=[Message.user("Say hi in one word.")],
            max_tokens=20,
        )
        assert r1.content

        r2 = await p.chat(
            model="qwen-plus-us",
            messages=[Message.user("Say hi in one word.")],
            max_tokens=20,
        )
        assert r2.content

        # Different models may give different responses
        assert isinstance(r1.content, str)
        assert isinstance(r2.content, str)

    async def test_temperature_variation(self):
        _need(self.ENV)
        p = get_llm("qwen-flash-us")

        resp_low = await p.chat(
            model="qwen-flash-us",
            messages=[Message.user("Say hello.")],
            temperature=0.0,
            max_tokens=20,
        )
        resp_high = await p.chat(
            model="qwen-flash-us",
            messages=[Message.user("Say hello.")],
            temperature=1.5,
            max_tokens=20,
        )
        assert resp_low.content
        assert resp_high.content
