"""Behavior of the MCP channel to the device.

The device answers ``tools/call`` on the same WebSocket that carries audio, so
a voice-driven volume change lands in tens of milliseconds instead of the
1-3s an MQTT c2d round trip would take. These tests drive the bridge through
its public interface: what it puts on the wire, what it returns for a device
answer, and what it does when the device does not answer.
"""

import asyncio

import pytest
from pipecat.processors.frame_processor import FrameDirection

from voice.pipecat.mcp_bridge import (
    McpToolError,
    McpToolTimeout,
    PiguguMcpBridge,
)
from voice.pipecat.pigugu_serializer import PiguguMessageFrame


def _device_reply(call_id, result):
    return {
        "type": "mcp",
        "payload": {"jsonrpc": "2.0", "id": call_id, "result": result},
    }


def _device_error(call_id, message):
    return {
        "type": "mcp",
        "payload": {"jsonrpc": "2.0", "id": call_id, "error": {"message": message}},
    }


def _text_result(payload_json: str):
    """The MCP envelope the device puts around a tool's answer."""
    return {
        "content": [{"type": "text", "text": payload_json}],
        "isError": False,
    }


class _Wire:
    """Captures what the bridge sends and lets a test play the device back."""

    def __init__(self, bridge):
        self.frames = []
        self._read = 0
        self._sent = asyncio.Event()
        bridge.push_frame = self._capture

    async def _capture(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.frames.append(frame)
        self._sent.set()

    async def next_request(self, timeout=1.0) -> dict:
        """Wait for the next outbound frame that has not been read yet."""
        while len(self.frames) <= self._read:
            await asyncio.wait_for(self._sent.wait(), timeout=timeout)
            self._sent.clear()
        message = self.frames[self._read].message
        self._read += 1
        return message

    def request_count(self) -> int:
        return len(self.frames)


@pytest.mark.asyncio
async def test_call_tool_reaches_the_device_and_returns_its_answer():
    bridge = PiguguMcpBridge()
    wire = _Wire(bridge)

    call = asyncio.create_task(
        bridge.call_tool("self.audio_speaker.set_volume", {"volume": 30})
    )
    request = await wire.next_request()

    assert request["type"] == "mcp"
    assert request["payload"]["method"] == "tools/call"
    assert request["payload"]["params"] == {
        "name": "self.audio_speaker.set_volume",
        "arguments": {"volume": 30},
    }

    await bridge.process_frame(
        PiguguMessageFrame(
            message=_device_reply(
                request["payload"]["id"], _text_result('{"volume": 30}')
            )
        ),
        FrameDirection.DOWNSTREAM,
    )

    assert await call == {"volume": 30}


@pytest.mark.asyncio
async def test_call_tool_raises_when_the_device_does_not_answer():
    bridge = PiguguMcpBridge()
    _Wire(bridge)

    with pytest.raises(McpToolTimeout):
        await bridge.call_tool("self.get_device_status", {}, timeout=0.05)


@pytest.mark.asyncio
async def test_call_tool_raises_the_device_error_message():
    bridge = PiguguMcpBridge()
    wire = _Wire(bridge)

    call = asyncio.create_task(
        bridge.call_tool("self.audio_speaker.set_volume", {"volume": 999})
    )
    request = await wire.next_request()

    await bridge.process_frame(
        PiguguMessageFrame(
            message=_device_error(request["payload"]["id"], "Value exceeds maximum allowed: 100")
        ),
        FrameDirection.DOWNSTREAM,
    )

    with pytest.raises(McpToolError, match="exceeds maximum"):
        await call


@pytest.mark.asyncio
async def test_concurrent_calls_resolve_by_id_not_by_arrival_order():
    bridge = PiguguMcpBridge()
    wire = _Wire(bridge)

    first = asyncio.create_task(bridge.call_tool("a", {}))
    first_request = await wire.next_request()
    second = asyncio.create_task(bridge.call_tool("b", {}))
    second_request = await wire.next_request()

    # The device answers the second call first.
    await bridge.process_frame(
        PiguguMessageFrame(
            message=_device_reply(second_request["payload"]["id"], _text_result('"B"'))
        ),
        FrameDirection.DOWNSTREAM,
    )
    await bridge.process_frame(
        PiguguMessageFrame(
            message=_device_reply(first_request["payload"]["id"], _text_result('"A"'))
        ),
        FrameDirection.DOWNSTREAM,
    )

    assert await first == "A"
    assert await second == "B"


@pytest.mark.asyncio
async def test_a_tool_error_result_raises_rather_than_returning_junk():
    bridge = PiguguMcpBridge()
    wire = _Wire(bridge)

    call = asyncio.create_task(bridge.call_tool("nope", {}))
    request = await wire.next_request()

    await bridge.process_frame(
        PiguguMessageFrame(
            message=_device_reply(
                request["payload"]["id"],
                {"content": [{"type": "text", "text": "Unknown tool: nope"}], "isError": True},
            )
        ),
        FrameDirection.DOWNSTREAM,
    )

    with pytest.raises(McpToolError, match="Unknown tool"):
        await call


@pytest.mark.asyncio
async def test_mcp_replies_do_not_leak_downstream():
    """Only the bridge consumes MCP frames; other processors must not see them."""
    bridge = PiguguMcpBridge()
    wire = _Wire(bridge)

    await bridge.process_frame(
        PiguguMessageFrame(message=_device_reply(999, _text_result("{}"))),
        FrameDirection.DOWNSTREAM,
    )

    assert wire.request_count() == 0


@pytest.mark.asyncio
async def test_a_non_mcp_frame_still_travels_downstream():
    bridge = PiguguMcpBridge()
    wire = _Wire(bridge)

    frame = PiguguMessageFrame(message={"type": "listen", "state": "start"})
    await bridge.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert wire.request_count() == 1
    assert wire.frames[0] is frame


@pytest.mark.asyncio
async def test_unanswered_calls_do_not_pile_up_after_timeout():
    """A timed-out id must be forgotten, or a late reply would resolve a dead call."""
    bridge = PiguguMcpBridge()
    wire = _Wire(bridge)

    with pytest.raises(McpToolTimeout):
        await bridge.call_tool("a", {}, timeout=0.05)
    request = await wire.next_request()

    assert bridge.pending_call_count() == 0

    # A reply for the abandoned id is ignored, not an error.
    await bridge.process_frame(
        PiguguMessageFrame(
            message=_device_reply(request["payload"]["id"], _text_result("{}"))
        ),
        FrameDirection.DOWNSTREAM,
    )


# ── Deferred dispatch ────────────────────────────────────────────────
#
# A change whose own feedback is the device's only signal has to wait for the
# reply to be heard: muting during the confirmation silences the confirmation.
# The bridge holds those calls; the TTS bridge flushes them at the end of the
# turn. See tools/volume.py for the mute that uses this.


@pytest.mark.asyncio
async def test_defer_holds_the_call_off_the_wire_until_flushed():
    bridge = PiguguMcpBridge()
    wire = _Wire(bridge)
    ran = []

    async def _device_call():
        ran.append(True)

    bridge.defer(_device_call, seq=bridge.note_write())

    assert bridge.has_deferred
    assert wire.request_count() == 0
    assert ran == []

    await bridge.flush_deferred()

    assert ran == [True]
    assert not bridge.has_deferred


@pytest.mark.asyncio
async def test_flush_clears_the_queue_so_a_second_flush_does_nothing():
    bridge = PiguguMcpBridge()
    ran = []

    async def _call():
        ran.append(True)

    bridge.defer(_call, seq=bridge.note_write())

    await bridge.flush_deferred()
    assert ran == [True]

    # A second flush is a no-op, not a repeat.
    await bridge.flush_deferred()
    assert ran == [True]
    assert not bridge.has_deferred


@pytest.mark.asyncio
async def test_a_failing_deferred_call_does_not_stop_the_others():
    """The turn is over and the user has already been told the change landed,
    so a failure is logged, never raised — and the rest of the batch still
    runs."""
    bridge = PiguguMcpBridge()
    ran = []
    seq = bridge.note_write()

    async def _boom():
        raise McpToolTimeout("no answer")

    async def _after():
        ran.append(True)

    # Same generation: neither supersedes the other, so both are current.
    bridge.defer(_boom, seq=seq)
    bridge.defer(_after, seq=seq)

    await bridge.flush_deferred()

    assert ran == [True]


@pytest.mark.asyncio
async def test_a_superseded_deferred_call_is_dropped():
    """A deferred write must not outlive a command issued after it: the user
    changing their mind mid-turn must not be overwritten by the older write."""
    bridge = PiguguMcpBridge()
    ran = []
    stale = bridge.note_write()

    async def _stale_call():
        ran.append("stale")

    async def _fresh_call():
        ran.append("fresh")

    bridge.defer(_stale_call, seq=stale)
    bridge.defer(_fresh_call, seq=bridge.note_write())

    await bridge.flush_deferred()

    assert ran == ["fresh"]
    assert not bridge.has_deferred


@pytest.mark.asyncio
async def test_a_failing_deferred_call_is_retried_on_later_flushes():
    """The alternative to a retry is a device left unmuted after the user was
    told it was muted. Bounded, so a dead device cannot accumulate entries."""
    bridge = PiguguMcpBridge()
    attempts = []

    async def _flaky():
        attempts.append(1)
        raise McpToolTimeout("no answer")

    bridge.defer(_flaky, seq=bridge.note_write())

    await bridge.flush_deferred()
    assert len(attempts) == 1
    assert bridge.has_deferred, "one transient failure must not end the retry"

    await bridge.flush_deferred()
    await bridge.flush_deferred()
    assert len(attempts) == 3
    assert not bridge.has_deferred, "bounded, not endless"


@pytest.mark.asyncio
async def test_teardown_can_bound_the_attempt_and_drop_it():
    """At teardown there is no later flush, so a failure is given up on rather
    than kept, and the call is bounded so cleanup cannot stall."""
    bridge = PiguguMcpBridge()
    attempts = []

    async def _never_answers():
        attempts.append(1)
        raise McpToolTimeout("no answer")

    bridge.defer(_never_answers, seq=bridge.note_write())

    await bridge.flush_deferred(timeout=0.05, requeue=False)

    assert len(attempts) == 1
    assert not bridge.has_deferred
