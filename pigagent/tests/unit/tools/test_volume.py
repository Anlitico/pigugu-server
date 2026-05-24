"""Tests for tools.volume — volume_tool definition."""

import asyncio

import pytest

from core.llm.types import ToolSpec
from tools.volume import volume_tool, _volume_handler
import tools.volume as _mod


def _reset_state():
    """Reset global volume state before tests that depend on it."""
    _mod._current_volume = 50
    _mod._muted = False


class TestVolumeTool:
    def test_tool_name(self):
        assert volume_tool.name == "volume_control"

    def test_description_hand_written(self):
        assert "audio volume" in volume_tool.description.lower()

    def test_parameters_action_enum(self):
        params = volume_tool.parameters
        assert params["type"] == "object"
        assert "action" in params["required"]
        assert "set" in params["properties"]["action"]["enum"]

    def test_spec_is_tool_spec(self):
        spec = volume_tool.spec
        assert isinstance(spec, ToolSpec)
        assert spec.name == "volume_control"

    def test_spec_to_openai_schema(self):
        schema = volume_tool.spec.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "volume_control"

    def test_execute_is_callable(self):
        assert callable(volume_tool.execute)
        assert volume_tool.execute is _volume_handler


class TestVolumeSet:
    def test_set_normal(self):
        _reset_state()
        result = asyncio.run(_volume_handler({"action": "set", "value": 80}))
        assert result["success"]
        assert result["level"] == 80

    def test_set_below_min(self):
        _reset_state()
        result = asyncio.run(_volume_handler({"action": "set", "value": -10}))
        assert result["success"]
        assert result["level"] == 0
        assert "cannot go below 0" in result["message"]

    def test_set_above_max(self):
        _reset_state()
        result = asyncio.run(_volume_handler({"action": "set", "value": 999}))
        assert result["success"]
        assert result["level"] == 100
        assert "cannot go above 100" in result["message"]

    def test_set_requires_value(self):
        result = asyncio.run(_volume_handler({"action": "set"}))
        assert not result["success"]


class TestVolumeIncrease:
    def test_increase_default_step(self):
        _reset_state()
        result = asyncio.run(_volume_handler({"action": "increase"}))
        assert result["success"]
        assert result["level"] == 55

    def test_increase_custom_step(self):
        _reset_state()
        result = asyncio.run(_volume_handler({"action": "increase", "value": 15}))
        assert result["success"]
        assert result["level"] == 65

    def test_increase_hits_max(self):
        _reset_state()
        _mod._current_volume = 95
        result = asyncio.run(_volume_handler({"action": "increase", "value": 10}))
        assert result["success"]
        assert result["level"] == 100
        assert "maximum" in result["message"]

    def test_increase_already_at_max_fails(self):
        _reset_state()
        _mod._current_volume = 100
        result = asyncio.run(_volume_handler({"action": "increase", "value": 5}))
        assert not result["success"]
        assert "already at maximum" in result["message"]


class TestVolumeDecrease:
    def test_decrease_default_step(self):
        _reset_state()
        result = asyncio.run(_volume_handler({"action": "decrease"}))
        assert result["success"]
        assert result["level"] == 45

    def test_decrease_hits_min(self):
        _reset_state()
        _mod._current_volume = 8
        result = asyncio.run(_volume_handler({"action": "decrease", "value": 10}))
        assert result["success"]
        assert result["level"] == 0
        assert "minimum" in result["message"]

    def test_decrease_already_at_min_fails(self):
        _reset_state()
        _mod._current_volume = 0
        result = asyncio.run(_volume_handler({"action": "decrease", "value": 5}))
        assert not result["success"]
        assert "already at minimum" in result["message"]


class TestVolumeMute:
    def test_mute(self):
        _reset_state()
        result = asyncio.run(_volume_handler({"action": "mute"}))
        assert result["success"]
        assert result["level"] == 0

    def test_unmute(self):
        _reset_state()
        _mod._current_volume = 60
        result = asyncio.run(_volume_handler({"action": "unmute"}))
        assert result["success"]
        assert result["level"] == 60

    def test_mute_then_unmute(self):
        _reset_state()
        _mod._current_volume = 70
        asyncio.run(_volume_handler({"action": "mute"}))
        result = asyncio.run(_volume_handler({"action": "unmute"}))
        assert result["level"] == 70


class TestVolumeEdgeCases:
    def test_unknown_action(self):
        result = asyncio.run(_volume_handler({"action": "destroy"}))
        assert not result["success"]

    def test_set_at_zero(self):
        _reset_state()
        result = asyncio.run(_volume_handler({"action": "set", "value": 0}))
        assert result["success"]
        assert result["level"] == 0

    def test_set_at_max(self):
        _reset_state()
        result = asyncio.run(_volume_handler({"action": "set", "value": 100}))
        assert result["success"]
        assert result["level"] == 100
