"""MCP (JSON-RPC) channel to the device, over the session's WebSocket.

The device advertises its tools and answers ``tools/call`` on the same socket
that carries audio, so a voice-driven volume change lands in tens of
milliseconds. The alternative — an MQTT c2d command — has to cross AWS IoT and
wait out the device's power-save beacon interval, which costs 1-3 seconds.

Outbound: :meth:`PiguguMcpBridge.call_tool` emits a JSON-RPC request and
awaits the matching reply. Inbound: a device frame with type ``mcp`` *is* that
reply; the bridge resolves the waiter and does not forward the frame, so no
other processor ever sees MCP traffic.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.pipecat.pigugu_serializer import PiguguMessageFrame, PiguguOutputMessageFrame

# The device answers on the same socket, so anything beyond this is a stall,
# not slowness. The voice path must fail fast enough to tell the user rather
# than leave the assistant claiming success.
DEFAULT_TOOL_TIMEOUT_SECS = 3.0

# How many flushes a deferred call may survive before it is given up on. The
# user was already told the change landed, so one transient failure must not be
# the end of it — but an entry that keeps failing must not accumulate forever.
MAX_DEFER_ATTEMPTS = 3


class McpToolError(RuntimeError):
    """The device rejected the call or reported a tool-level error."""


class McpToolTimeout(RuntimeError):
    """The device did not answer the call in time."""


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _result_text(result: dict) -> str | None:
    """The first text block of an MCP result envelope, if any."""
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            return item.get("text")
    return None


def _unwrap_result(result: Any) -> Any:
    """Pull the tool's answer out of the MCP content envelope.

    The device wraps every tool answer as a single text block, and by its own
    convention the payload inside that text is JSON (``get_device_status``
    puts a document there, ``set_volume`` a small object). Decoding it here
    keeps that convention in one place instead of at every call site.
    """
    if not isinstance(result, dict):
        return result
    text = _result_text(result)
    if result.get("isError"):
        raise McpToolError(text or "device reported a tool error")
    if text is None:
        return result
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        # Not every tool answers with JSON; hand back the plain text.
        return text


class PiguguMcpBridge(FrameProcessor):
    """Calls the device's MCP tools and correlates their replies."""

    def __init__(self, *, default_timeout: float = DEFAULT_TOOL_TIMEOUT_SECS, **kwargs):
        super().__init__(**kwargs)
        self._default_timeout = default_timeout
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._deferred: list[tuple[int, int, Callable[[], Awaitable[Any]]]] = []
        self._write_seq = 0

    async def call_tool(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call a device tool and return its answer.

        Raises :class:`McpToolTimeout` if the device stays silent and
        :class:`McpToolError` if it refuses the call. Never returns a value
        the device did not send.
        """
        call_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future

        await self.push_frame(
            PiguguOutputMessageFrame(
                message={
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": call_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments or {}},
                    },
                }
            )
        )

        wait = self._default_timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(future, wait)
        except asyncio.TimeoutError as exc:
            logger.warning("[McpBridge] {} got no answer within {}s", name, wait)
            raise McpToolTimeout(f"{name} did not answer within {wait}s") from exc
        finally:
            # Forget the id either way: a late reply must not resolve a call
            # whose caller has already given up.
            self._pending.pop(call_id, None)

    def pending_call_count(self) -> int:
        return len(self._pending)

    def note_write(self) -> int:
        """Claim the next write generation for a volume command.

        A deferred write only lands at the end of the turn, so without this a
        command the user issued afterwards — changing their mind, in the same
        turn — would be overwritten by the older one still waiting. Callers
        stamp their deferred work with the value returned here.
        """
        self._write_seq += 1
        return self._write_seq

    def defer(self, call: Callable[[], Awaitable[Any]], *, seq: int) -> None:
        """Queue a device call to dispatch once the reply has been heard.

        For a change whose own feedback is the only signal the device has:
        going silent silences the assistant's confirmation along with
        everything else, so the write waits until that confirmation has played.
        The queue is drained by the TTS bridge at the end of the turn; a call
        superseded by a later command is dropped rather than applied.
        """
        self._deferred.append((seq, MAX_DEFER_ATTEMPTS, call))

    @property
    def has_deferred(self) -> bool:
        return bool(self._deferred)

    async def flush_deferred(
        self, *, timeout: float | None = None, requeue: bool = True
    ) -> None:
        """Dispatch and clear the deferred calls.

        Never raises: the turn is over and the user has already been told the
        change landed, so a failure here can only be logged loudly — there is
        no one left to take it back. A failure is kept for a later flush
        (bounded by :data:`MAX_DEFER_ATTEMPTS`) unless the caller says
        otherwise, because the alternative is a device left unmuted.

        ``timeout`` bounds each call, for a flush that must not stall the
        caller; ``requeue=False`` drops failures, for teardown where there is
        no later flush to hand them to.
        """
        pending, self._deferred = self._deferred, []
        for seq, attempts, call in pending:
            if seq != self._write_seq:
                logger.info(
                    "[McpBridge] dropping a superseded deferred call ({} != {})",
                    seq,
                    self._write_seq,
                )
                continue
            try:
                if timeout is None:
                    await call()
                else:
                    await asyncio.wait_for(call(), timeout)
                continue
            except Exception:
                logger.exception(
                    "[McpBridge] deferred device call failed (attempt {}/{})",
                    MAX_DEFER_ATTEMPTS - attempts + 1,
                    MAX_DEFER_ATTEMPTS,
                )
            if requeue and attempts > 1:
                self._deferred.append((seq, attempts - 1, call))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, PiguguMessageFrame) and frame.message.get("type") == "mcp":
            self._resolve(frame.message.get("payload"))
            return
        await self.push_frame(frame, direction)

    def _resolve(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            logger.warning("[McpBridge] mcp frame without a payload object, dropped")
            return
        call_id = payload.get("id")
        future = self._pending.get(call_id)
        if future is None or future.done():
            # Not one of ours, or already timed out — nothing to resolve.
            logger.debug("[McpBridge] mcp reply for unknown id={} dropped", call_id)
            return
        if payload.get("error") is not None:
            future.set_exception(McpToolError(_error_message(payload["error"])))
            return
        try:
            value = _unwrap_result(payload.get("result"))
        except McpToolError as exc:
            future.set_exception(exc)
            return
        future.set_result(value)
