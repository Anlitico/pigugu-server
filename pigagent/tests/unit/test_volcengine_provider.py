# tests/unit/test_volcengine_provider.py
"""Unit tests for VolcengineProvider  -  validation, thinking format, message serialization."""

import pytest

from core.llm.registry import ModelRegistry, load_models
from core.llm.providers.volcengine import VolcengineProvider
from core.llm.types import Message, ModelInfo, ModelCapability, ToolCall


# -- Helpers ------------------------------------------------------------------

def _register_model(model_id="doubao-test", *, thinking=True, search=True,
                    caps=None, provider="volcengine"):
    ModelRegistry.register(ModelInfo(
        model_id=model_id,
        provider=provider,
        display_name=model_id,
        capabilities=caps or {ModelCapability.TEXT, ModelCapability.STREAMING,
                              ModelCapability.TOOL_USE},
        context_window=256000,
        max_output_tokens=16384,
        thinking=thinking,
        search=search,
    ))


# -- Validation tests ---------------------------------------------------------

class TestVolcengineValidation:
    def setup_method(self):
        ModelRegistry._models.clear()

    def teardown_method(self):
        load_models()

    def test_thinking_supported(self):
        _register_model(thinking=True)
        p = VolcengineProvider(api_key="sk-test")
        p._validate(tools=None, thinking={"enabled": True}, search=None, model="doubao-test")

    def test_thinking_not_supported_raises(self):
        _register_model(thinking=False)
        p = VolcengineProvider(api_key="sk-test")
        with pytest.raises(ValueError, match="does not support thinking"):
            p._validate(tools=None, thinking={"enabled": True}, search=None, model="doubao-test")

    def test_search_not_supported_raises(self):
        _register_model(search=False)
        p = VolcengineProvider(api_key="sk-test")
        with pytest.raises(ValueError, match="does not support web search"):
            p._validate(tools=None, thinking=None, search={"enabled": True}, model="doubao-test")

    def test_tool_use_not_supported_raises(self):
        _register_model(caps={ModelCapability.TEXT})
        p = VolcengineProvider(api_key="sk-test")
        with pytest.raises(ValueError, match="does not support tool_use"):
            p._validate(tools=[{"type": "function"}], thinking=None, search=None, model="doubao-test")


# -- Thinking parameter format (Volcengine-specific) --------------------------

class TestVolcengineThinkingFormat:
    def setup_method(self):
        ModelRegistry._models.clear()
        _register_model()

    def teardown_method(self):
        load_models()

    def _make_provider(self):
        return VolcengineProvider(api_key="sk-test")

    def test_thinking_enabled(self):
        p = self._make_provider()
        params = p._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None,
            {"enabled": True}, None, None,
            model="doubao-test", stream=False,
        )
        assert params["extra_body"]["thinking"] == {"type": "enabled"}

    def test_thinking_with_budget(self):
        p = self._make_provider()
        params = p._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None,
            {"enabled": True, "budget": 16000}, None, None,
            model="doubao-test", stream=False,
        )
        assert params["extra_body"]["thinking"] == {
            "type": "enabled",
            "budget_tokens": 16000,
        }

    def test_thinking_with_effort(self):
        p = self._make_provider()
        params = p._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None,
            {"enabled": True, "effort": "high"}, None, None,
            model="doubao-test", stream=False,
        )
        assert params["extra_body"]["thinking"]["reasoning_effort"] == "high"

    def test_thinking_full(self):
        p = self._make_provider()
        params = p._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None,
            {"enabled": True, "budget": 32000, "effort": "medium"}, None, None,
            model="doubao-test", stream=False,
        )
        assert params["extra_body"]["thinking"] == {
            "type": "enabled",
            "budget_tokens": 32000,
            "reasoning_effort": "medium",
        }

    def test_thinking_disabled_not_in_body(self):
        p = self._make_provider()
        params = p._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None,
            {"enabled": False}, None, None,
            model="doubao-test", stream=False,
        )
        assert "extra_body" not in params


# -- Message serialization ----------------------------------------------------

class TestVolcengineSerialization:
    def test_prefix_on_partial_assistant(self):
        p = VolcengineProvider(api_key="sk-test")
        msg = Message(role="assistant", content="prefix...", partial=True)
        d = p._serialize_message(msg)
        assert d["prefix"] is True

    def test_no_prefix_on_normal_message(self):
        p = VolcengineProvider(api_key="sk-test")
        msg = Message(role="assistant", content="complete")
        d = p._serialize_message(msg)
        assert "prefix" not in d

    def test_no_prefix_on_user_message(self):
        p = VolcengineProvider(api_key="sk-test")
        msg = Message(role="user", content="hello", partial=True)
        d = p._serialize_message(msg)
        assert "prefix" not in d
