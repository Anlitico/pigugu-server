# tests/unit/test_xai_provider.py
"""Unit tests for XaiProvider — reasoning_effort mapping, web search, params, usage."""

import pytest

from core.llm.registry import ModelRegistry, load_models
from core.llm.providers.xai import XaiProvider
from core.llm.types import Message, ModelInfo, ModelCapability


# -- Helpers ------------------------------------------------------------------

def _register_model(model_id="grok-4-3", *, thinking=True, search=True,
                    caps=None):
    ModelRegistry.register(ModelInfo(
        model_id=model_id,
        provider="xai",
        display_name=model_id,
        capabilities=caps or {ModelCapability.TEXT, ModelCapability.STREAMING,
                              ModelCapability.TOOL_USE},
        context_window=1000000,
        max_output_tokens=16384,
        thinking=thinking,
        search=search,
    ))


# -- Fixture ------------------------------------------------------------------

@pytest.fixture
def xai_provider():
    """Return a XaiProvider with no API key (unit-test safe)."""
    return XaiProvider(api_key="sk-test", base_url="https://api.x.ai/v1")


# -- BuildParams tests --------------------------------------------------------

class TestXaiBuildParams:
    def setup_method(self):
        ModelRegistry._models.clear()
        _register_model()

    def teardown_method(self):
        load_models()

    def test_basic_params(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            None, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["model"] == "grok-4-3"
        assert params["temperature"] == 0.6
        assert params["stream"] is False

    def test_streaming(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            None, None, None,
            model="grok-4-3", stream=True,
        )
        assert params["stream"] is True
        assert params["stream_options"] == {"include_usage": True}

    def test_tools(self, xai_provider):
        tools = [{"type": "function", "function": {"name": "test"}}]
        params = xai_provider._build_params(
            [Message.user("hello")], tools, None, True,
            None, None, None, None, None,
            None, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["tools"] == tools
        assert params["parallel_tool_calls"] is True

    def test_tool_choice(self, xai_provider):
        tools = [{"type": "function", "function": {"name": "test"}}]
        params = xai_provider._build_params(
            [Message.user("hello")], tools, "required", True,
            None, None, None, None, None,
            None, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["tool_choice"] == "required"

    def test_tool_choice_auto_omitted(self, xai_provider):
        tools = [{"type": "function", "function": {"name": "test"}}]
        params = xai_provider._build_params(
            [Message.user("hello")], tools, "auto", True,
            None, None, None, None, None,
            None, None, None,
            model="grok-4-3", stream=False,
        )
        assert "tool_choice" not in params

    def test_max_tokens_passthrough(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, 200, None, None,
            None, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["max_tokens"] == 200

    def test_stop_and_seed(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, ["STOP"], 42,
            None, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["stop"] == ["STOP"]
        assert params["seed"] == 42

    # -- Reasoning mapping --

    def test_reasoning_disabled_not_in_body(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            {"enabled": False}, None, None,
            model="grok-4-3", stream=False,
        )
        assert "reasoning_effort" not in params

    def test_reasoning_enabled_default_low(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            {"enabled": True}, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["reasoning_effort"] == "low"

    def test_reasoning_high_budget(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            {"enabled": True, "budget": 32000}, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["reasoning_effort"] == "high"

    def test_reasoning_medium_budget(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            {"enabled": True, "budget": 16000}, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["reasoning_effort"] == "medium"

    def test_reasoning_low_budget(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            {"enabled": True, "budget": 4000}, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["reasoning_effort"] == "low"

    def test_reasoning_non_int_budget_falls_back(self, xai_provider):
        """Non-int budget (e.g. string) falls back to low."""
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            {"enabled": True, "budget": "maximum"}, None, None,
            model="grok-4-3", stream=False,
        )
        assert params["reasoning_effort"] == "low"

    # -- Web search mapping --

    def test_search_enabled(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            None, {"enabled": True}, None,
            model="grok-4-3", stream=False,
        )
        assert "web_search_options" in params
        assert params["web_search_options"] == {"search_context_size": "medium"}

    def test_search_disabled(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            None, {"enabled": False}, None,
            model="grok-4-3", stream=False,
        )
        assert "web_search_options" not in params

    # -- Response format --

    def test_response_format(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            None, None, {"type": "json_object"},
            model="grok-4-3", stream=False,
        )
        assert params["response_format"] == {"type": "json_object"}

    # -- Extra kwargs --

    def test_extra_kwargs_passthrough(self, xai_provider):
        params = xai_provider._build_params(
            [Message.user("hello")], None, None, True,
            None, None, None, None, None,
            None, None, None,
            model="grok-4-3", stream=False,
            custom_param=42,
        )
        assert params["custom_param"] == 42


# -- Usage extraction ---------------------------------------------------------

class TestXaiUsage:
    def test_extract_from_dict(self):
        """Streaming chunks return usage as dict."""
        u = XaiProvider._extract_usage({
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "total_tokens": 280,
        })
        assert u.prompt_tokens == 200
        assert u.completion_tokens == 80
        assert u.total_tokens == 280
        assert u.cached_prompt_tokens == 0  # xAI doesn't use prompt_tokens_details

    def test_extract_from_object(self):
        """Non-streaming response usage is an object."""
        from core.llm.types import TokenUsage as TU

        class UsageObj:
            prompt_tokens = 100
            completion_tokens = 50
            total_tokens = 150

        u = XaiProvider._extract_usage(UsageObj())
        assert u.prompt_tokens == 100
        assert u.completion_tokens == 50
        assert u.total_tokens == 150


# -- Registry resolution ------------------------------------------------------

class TestXaiRegistry:
    def test_grok_4_3_is_registered(self):
        """grok-4-3 should be in ModelRegistry after load_models()."""
        load_models()
        info = ModelRegistry.get("grok-4-3")
        assert info.provider == "xai"
        assert info.context_window == 1000000
        assert ModelCapability.TOOL_USE in info.capabilities
        assert info.thinking is True
        assert info.search is True
