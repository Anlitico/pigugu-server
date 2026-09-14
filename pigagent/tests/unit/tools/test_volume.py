"""Tests for tools.volume  -  volume_tool definition and device behaviour.

The tool talks to the device over the session's MCP channel and reports what
the device echoes back. These tests pin the property that made the old
implementation a bug: it must never report success the device did not confirm.
"""

import asyncio

from core.llm.types import ToolSpec
from tools.volume import volume_tool, _volume_handler
from voice.pipecat.mcp_bridge import McpToolError, McpToolTimeout
import tools.volume as _mod


class _FakeBridge:
    """A device that answers MCP tools/call the way the firmware does."""

    def __init__(
        self,
        volume: int = 70,
        fail: Exception | None = None,
        echo_level: bool = True,
        fail_status: Exception | None = None,
    ):
        self.volume = volume
        self.fail = fail
        self.echo_level = echo_level
        self.fail_status = fail_status
        self.calls: list[tuple[str, dict]] = []
        self._deferred: list = []
        self._write_seq = 0

    def note_write(self) -> int:
        self._write_seq += 1
        return self._write_seq

    def defer(self, call, *, seq: int) -> None:
        self._deferred.append((seq, call))

    @property
    def has_deferred(self) -> bool:
        return bool(self._deferred)

    async def flush_deferred(self) -> None:
        pending, self._deferred = self._deferred, []
        for seq, call in pending:
            if seq != self._write_seq:
                continue
            await call()

    async def call_tool(self, name, arguments=None, *, timeout=None):
        self.calls.append((name, arguments or {}))
        if self.fail is not None:
            raise self.fail
        if name == "self.get_device_status":
            if self.fail_status is not None:
                raise self.fail_status
            return {"audio_speaker": {"volume": self.volume}}
        if name == "self.audio_speaker.set_volume":
            self.volume = arguments["volume"]
            if not self.echo_level:
                return True  # firmware from before the level echo
            return {"volume": self.volume}
        raise AssertionError(f"unexpected tool: {name}")

    def tools_called(self) -> list[str]:
        return [name for name, _ in self.calls]


def _with_device(bridge):
    """Point the tool at `bridge`."""
    return _mod._current_mcp.set(bridge)


def _run(args):
    return asyncio.run(_volume_handler(args))


class TestVolumeToolDefinition:
    def test_tool_name(self):
        assert volume_tool.name == "volume_control"

    def test_description_hand_written(self):
        assert "audio volume" in volume_tool.description.lower()

    def test_parameters_action_enum(self):
        params = volume_tool.parameters
        assert params["type"] == "object"
        assert "action" in params["required"]
        assert "set" in params["properties"]["action"]["enum"]

    def test_filler_text_is_required_like_every_other_tool(self):
        """global.j2 promises the model that every tool takes filler_text, and
        the runner speaks it while the tool runs. A tool that omits it gets
        skipped instead of called — measured on qwen-flash: 2/9 vs 6/9."""
        assert "filler_text" in volume_tool.parameters.get("required", [])

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


class TestNoDevice:
    def test_reports_failure_rather_than_pretending(self):
        token = _mod._current_mcp.set(None)
        try:
            result = _run({"action": "set", "value": 50})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is False
        assert "No device" in result["message"]


class TestSet:
    def test_set_reports_the_level_the_device_echoed(self):
        bridge = _FakeBridge(volume=70)
        token = _with_device(bridge)
        try:
            result = _run({"action": "set", "value": 30})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is True
        assert result["level"] == 30
        assert bridge.volume == 30
        assert bridge.tools_called() == ["self.audio_speaker.set_volume"]

    def test_set_on_old_firmware_reads_the_level_back(self):
        """A device that predates the level echo answers with a bare success.

        The write still landed, so the level must be read back rather than
        reported as a failure the device never had — otherwise the fleet says
        "the volume was not changed" while the volume audibly changes.
        """
        bridge = _FakeBridge(volume=70, echo_level=False)
        token = _with_device(bridge)
        try:
            result = _run({"action": "set", "value": 30})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is True
        assert result["level"] == 30
        assert bridge.volume == 30
        assert bridge.tools_called() == [
            "self.audio_speaker.set_volume",
            "self.get_device_status",
        ]

    def test_a_bare_success_with_an_unreadable_level_still_counts_as_applied(self):
        """The write landed; only the read-back failed.

        Reporting a failure here would be the same false negative the read-back
        exists to remove.
        """
        bridge = _FakeBridge(
            volume=70, echo_level=False, fail_status=McpToolTimeout("no answer")
        )
        token = _with_device(bridge)
        try:
            result = _run({"action": "set", "value": 30})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is True
        assert result["level"] == 30
        assert bridge.volume == 30

    def test_set_above_max_is_clamped_and_said_so(self):
        bridge = _FakeBridge()
        token = _with_device(bridge)
        try:
            result = _run({"action": "set", "value": 150})
        finally:
            _mod._current_mcp.reset(token)

        assert result["level"] == 100
        assert "100" in result["message"]

    def test_set_without_value_fails(self):
        bridge = _FakeBridge()
        token = _with_device(bridge)
        try:
            result = _run({"action": "set"})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is False
        assert bridge.tools_called() == []


class TestRelative:
    def test_increase_uses_the_devices_real_level(self):
        """The target must come from the device, not a server-side guess."""
        bridge = _FakeBridge(volume=40)
        token = _with_device(bridge)
        try:
            result = _run({"action": "increase"})
        finally:
            _mod._current_mcp.reset(token)

        assert result["level"] == 45
        assert bridge.tools_called() == [
            "self.get_device_status",
            "self.audio_speaker.set_volume",
        ]

    def test_decrease_that_bottoms_out_is_deferred_like_mute(self):
        """The same write as mute, reached a different way: going to 0 would
        take the spoken confirmation down with it."""
        bridge = _FakeBridge(volume=3)
        token = _with_device(bridge)
        try:
            result = _run({"action": "decrease", "value": 10})
            assert bridge.volume == 3, "must not go silent mid-confirmation"
            assert bridge.has_deferred
            asyncio.run(bridge.flush_deferred())
        finally:
            _mod._current_mcp.reset(token)

        assert result["level"] == 0
        assert bridge.volume == 0

    def test_set_to_zero_is_deferred_like_mute(self):
        bridge = _FakeBridge(volume=60)
        token = _with_device(bridge)
        try:
            result = _run({"action": "set", "value": 0})
            assert bridge.volume == 60, "must not go silent mid-confirmation"
            assert bridge.has_deferred
            asyncio.run(bridge.flush_deferred())
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is True
        assert result["level"] == 0
        assert bridge.volume == 0

    def test_a_failed_command_does_not_drop_a_pending_mute(self):
        """The user was told the device is muted; a later command that never
        reached the device must not quietly cancel that."""
        bridge = _FakeBridge(volume=60)
        token = _with_device(bridge)
        try:
            _run({"action": "mute"})
            # A later command in the same turn fails before touching the device.
            bridge.fail = McpToolTimeout("no answer")
            _run({"action": "decrease"})
            bridge.fail = None
            asyncio.run(bridge.flush_deferred())
        finally:
            _mod._current_mcp.reset(token)

        assert bridge.volume == 0, "the mute the user confirmed must still land"

    def test_a_later_command_in_the_same_turn_supersedes_a_deferred_mute(self):
        """The user changes their mind mid-turn: the mute must not land after
        the assistant has already told them the volume is fine."""
        bridge = _FakeBridge(volume=60)
        token = _with_device(bridge)
        try:
            _run({"action": "mute"})
            _run({"action": "unmute"})  # claims a newer write generation
            asyncio.run(bridge.flush_deferred())
        finally:
            _mod._current_mcp.reset(token)

        assert bridge.volume == 60, "the superseded mute must not land"

    def test_increase_at_maximum_does_not_touch_the_device(self):
        bridge = _FakeBridge(volume=100)
        token = _with_device(bridge)
        try:
            result = _run({"action": "increase"})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is False
        assert bridge.tools_called() == ["self.get_device_status"]


    def test_a_negative_step_cannot_turn_increase_into_a_decrease(self):
        """The schema describes 5/10/15, but the model can send anything: a
        negative step would otherwise compute a target below 0 and hand the
        device a value its own tool property rejects."""
        bridge = _FakeBridge(volume=10)
        token = _with_device(bridge)
        try:
            result = _run({"action": "increase", "value": -50})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is True
        assert result["level"] == 11, "a step is a positive amount, never a reversal"

    def test_a_negative_step_cannot_turn_decrease_into_an_increase(self):
        bridge = _FakeBridge(volume=90)
        token = _with_device(bridge)
        try:
            result = _run({"action": "decrease", "value": -50})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is True
        assert result["level"] == 89, "a step is a positive amount, never a reversal"


class TestMute:
    def test_mute_defers_until_the_confirmation_has_been_heard(self):
        """The spoken confirmation is the only feedback this device has, so the
        write must not land before the user has heard it."""
        bridge = _FakeBridge(volume=60)
        token = _with_device(bridge)
        try:
            result = _run({"action": "mute"})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is True
        assert result["level"] == 0
        assert bridge.volume == 60, "must not go silent mid-confirmation"
        assert bridge.has_deferred

    def test_the_deferred_mute_silences_the_device_once_flushed(self):
        bridge = _FakeBridge(volume=60)
        token = _with_device(bridge)
        try:
            _run({"action": "mute"})
            asyncio.run(bridge.flush_deferred())
        finally:
            _mod._current_mcp.reset(token)

        assert bridge.volume == 0
        assert bridge.tools_called() == ["self.audio_speaker.set_volume"]

    def test_unmute_brings_a_silent_device_back_at_the_default_level(self):
        bridge = _FakeBridge(volume=0)
        token = _with_device(bridge)
        try:
            result = _run({"action": "unmute"})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is True
        assert result["level"] == _mod.UNMUTE_LEVEL
        assert bridge.volume == _mod.UNMUTE_LEVEL

    def test_unmute_leaves_an_audible_device_alone(self):
        bridge = _FakeBridge(volume=35)
        token = _with_device(bridge)
        try:
            result = _run({"action": "unmute"})
        finally:
            _mod._current_mcp.reset(token)

        assert result["level"] == 35
        assert bridge.tools_called() == ["self.get_device_status"]


class TestDeviceFailures:
    def test_a_silent_device_is_a_failure_not_a_success(self):
        bridge = _FakeBridge(fail=McpToolTimeout("no answer"))
        token = _with_device(bridge)
        try:
            result = _run({"action": "set", "value": 30})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is False
        assert "did not respond" in result["message"]

    def test_a_refused_call_surfaces_the_device_message(self):
        bridge = _FakeBridge(fail=McpToolError("Value exceeds maximum allowed: 100"))
        token = _with_device(bridge)
        try:
            result = _run({"action": "set", "value": 30})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is False
        assert "exceeds maximum" in result["message"]

    def test_a_relative_action_fails_when_the_level_cannot_be_read(self):
        class _Unreadable:
            def note_write(self) -> int:
                return 1

            async def call_tool(self, name, arguments=None, *, timeout=None):
                raise KeyError("audio_speaker")

        token = _with_device(_Unreadable())
        try:
            result = _run({"action": "increase"})
        finally:
            _mod._current_mcp.reset(token)

        assert result["success"] is False
        assert "did not respond" in result["message"]
