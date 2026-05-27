# tests/unit/test_qwen_provider.py
"""Unit tests for QwenProvider  -  validation, parameter mapping, message serialization."""

import pytest

from core.llm.registry import ModelRegistry, load_models
from core.llm.providers.qwen import QwenProvider
from core.llm.types import (
    Message, ModelInfo, ModelCapability, ToolCall, TokenUsage, ChatResponse
)


# -- Helpers ------------------------------------------------------------------

def _register_model(model_id="qwen-plus", *, thinking=True, search=True,
                    caps=None, provider="qwen-us"):
    ModelRegistry.register(ModelInfo(
        model_id=model_id,
        provider=provider,
        display_name=model_id,
        capabilities=caps or {ModelCapability.TEXT, ModelCapability.STREAMING,
                              ModelCapability.TOOL_USE, ModelCapability.WEB_SEARCH},
        context_window=1000000,
        max_output_tokens=32768,
        thinking=thinking,
        search=search,
    ))


# -- Validation tests ---------------------------------------------------------

class TestQwenValidation:
    def setup_method(self):
        ModelRegistry._models.clear()

    def teardown_method(self):
        load_models()

    def test_thinking_supported(self, qwen_provider):
        _register_model("qwen-plus", thinking=True)
        qwen_provider._validate(tools=None, thinking={"enabled": True}, search=None, model="qwen-plus")

    def test_thinking_not_supported_raises(self, qwen_provider):
        _register_model("qwen-turbo", thinking=False)
        with pytest.raises(ValueError, match="does not support thinking"):
            qwen_provider._validate(tools=None, thinking={"enabled": True}, search=None, model="qwen-turbo")

    def test_thinking_disabled_does_not_raise(self, qwen_provider):
        _register_model("qwen-turbo", thinking=False)
        qwen_provider._validate(tools=None, thinking={"enabled": False}, search=None, model="qwen-turbo")

    def test_search_supported(self, qwen_provider):
        _register_model("qwen-plus", search=True)
        qwen_provider._validate(tools=None, thinking=None, search={"enabled": True}, model="qwen-plus")

    def test_search_not_supported_raises(self, qwen_provider):
        _register_model("qwen-turbo", search=False)
        with pytest.raises(ValueError, match="does not support web search"):
            qwen_provider._validate(tools=None, thinking=None, search={"enabled": True}, model="qwen-turbo")

    def test_tool_use_supported(self, qwen_provider):
        _register_model("qwen-plus", caps={ModelCapability.TEXT, ModelCapability.TOOL_USE})
        qwen_provider._validate(tools=[{"type": "function", "function": {"name": "test"}}],
                    thinking=None, search=None, model="qwen-plus")

    def test_tool_use_not_supported_raises(self, qwen_provider):
        _register_model("qwen-turbo", caps={ModelCapability.TEXT})
        with pytest.raises(ValueError, match="does not support tool_use"):
            qwen_provider._validate(tools=[{"type": "function"}], thinking=None, search=None, model="qwen-turbo")

    def test_all_none_passes(self, qwen_provider):
        _register_model("qwen-turbo", thinking=False, search=False,
                        caps={ModelCapability.TEXT})
        qwen_provider._validate(tools=None, thinking=None, search=None, model="qwen-turbo")


# -- Parameter mapping tests --------------------------------------------------

class TestQwenBuildParams:
    def setup_method(self):
        ModelRegistry._models.clear()
        _register_model("qwen-plus")

    def teardown_method(self):
        load_models()

    def test_basic_params(self, qwen_provider):
        params = qwen_provider._build_params(
            [Message.user("hi")], None, None, True,
            0.7, None, 512, None, None, None, None, None,
            model="qwen-plus", stream=False,
        )
        assert params["model"] == "qwen-plus"
        assert params["temperature"] == 0.7
        assert params["max_tokens"] == 512
        assert not params["stream"]

    def test_streaming(self, qwen_provider):
        params = qwen_provider._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None, None, None, None,
            model="qwen-plus", stream=True,
        )
        assert params["stream"]
        assert params["stream_options"] == {"include_usage": True}

    def test_tools(self, qwen_provider):
        tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
        params = qwen_provider._build_params(
            [Message.user("hi")], tools, None, False,
            None, None, None, None, None, None, None, None,
            model="qwen-plus", stream=False,
        )
        assert params["tools"] == tools
        assert not params["parallel_tool_calls"]

    def test_tool_choice(self, qwen_provider):
        params = qwen_provider._build_params(
            [Message.user("hi")], [{"type": "function"}], "required", True,
            None, None, None, None, None, None, None, None,
            model="qwen-plus", stream=False,
        )
        assert params["tool_choice"] == "required"

    def test_thinking_params(self, qwen_provider):
        params = qwen_provider._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None,
            {"enabled": True, "budget": 4096}, None, None,
            model="qwen-plus", stream=False,
        )
        assert params["extra_body"]["enable_thinking"] is True
        assert params["extra_body"]["thinking_budget"] == 4096

    def test_search_basic(self, qwen_provider):
        params = qwen_provider._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None, None,
            {"enabled": True, "force": False}, None,
            model="qwen-plus", stream=False,
        )
        assert params["extra_body"]["enable_search"] is True
        assert "search_options" not in params["extra_body"]

    def test_search_force(self, qwen_provider):
        params = qwen_provider._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None, None,
            {"enabled": True, "force": True}, None,
            model="qwen-plus", stream=False,
        )
        assert params["extra_body"]["enable_search"] is True
        assert params["extra_body"]["search_options"] == {"search_strategy": "agent"}

    def test_response_format(self, qwen_provider):
        fmt = {"type": "json_object"}
        params = qwen_provider._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None, None, None, fmt,
            model="qwen-plus", stream=False,
        )
        assert params["response_format"] == fmt

    def test_stop_and_seed(self, qwen_provider):
        params = qwen_provider._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, ["END"], 42, None, None, None,
            model="qwen-plus", stream=False,
        )
        assert params["stop"] == ["END"]
        assert params["seed"] == 42

    def test_max_tokens_passthrough(self, qwen_provider):
        params = qwen_provider._build_params(
            [Message.user("hi")], None, None, True,
            None, None, 2048, None, None, None, None, None,
            model="qwen-plus", stream=False,
        )
        assert params["max_tokens"] == 2048

    def test_extra_kwargs(self, qwen_provider):
        params = qwen_provider._build_params(
            [Message.user("hi")], None, None, True,
            None, None, None, None, None, None, None, None,
            model="qwen-plus", stream=False, custom_param="value",
        )
        assert params["extra_body"]["custom_param"] == "value"


# -- Message serialization ----------------------------------------------------

class TestQwenSerialization:
    def test_partial_assistant(self, qwen_provider):
        msg = Message(role="assistant", content="prefix...", partial=True)
        d = qwen_provider._serialize_message(msg)
        assert d["partial"] is True
        assert d["role"] == "assistant"

    def test_normal_assistant(self, qwen_provider):
        msg = Message(role="assistant", content="complete")
        d = qwen_provider._serialize_message(msg)
        assert "partial" not in d

    def test_user_message(self, qwen_provider):
        msg = Message(role="user", content="hello")
        d = qwen_provider._serialize_message(msg)
        assert d["role"] == "user"
        assert d["content"] == "hello"

    def test_tool_calls(self, qwen_provider):
        msg = Message(role="assistant", content="",
                      tool_calls=[ToolCall(id="c1", name="search", arguments='{"q":"test"}')])
        d = qwen_provider._serialize_message(msg)
        assert len(d["tool_calls"]) == 1
        assert d["tool_calls"][0]["function"]["name"] == "search"


# -- Usage extraction ---------------------------------------------------------

class TestQwenUsage:
    def test_extract_basic(self):
        class MockUsage:
            prompt_tokens = 100
            completion_tokens = 50
            total_tokens = 150

        u = QwenProvider._extract_usage(MockUsage())
        assert u.prompt_tokens == 100
        assert u.completion_tokens == 50
        assert u.total_tokens == 150
        assert u.cached_prompt_tokens == 0

    def test_extract_with_cache(self):
        class MockDetails:
            cached_tokens = 30

        class MockUsage:
            prompt_tokens = 100
            completion_tokens = 50
            total_tokens = 150
            prompt_tokens_details = MockDetails()

        u = QwenProvider._extract_usage(MockUsage())
        assert u.cached_prompt_tokens == 30

    def test_extract_from_dict(self):
        """Streaming chunks return usage as dict, not object."""
        u = QwenProvider._extract_usage({
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "total_tokens": 280,
        })
        assert u.prompt_tokens == 200
        assert u.completion_tokens == 80
        assert u.cached_prompt_tokens == 0

    def test_extract_from_dict_with_cache(self):
        u = QwenProvider._extract_usage({
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 64},
        })
        assert u.cached_prompt_tokens == 64
