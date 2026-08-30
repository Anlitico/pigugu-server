"""TTS bridge — LLM reply → Cartesia streaming TTS → paced Opus frames.

Consumes ``PiguguUserTurnFrame``, runs ``PigAgent.generate_reply``, feeds the
text stream through our Cartesia provider (which already emits Opus — no
re-encode), paces the frames out with the virtual playback clock (ported from
connection.py), and drives the device tts/start · stop · abort protocol.

Shared turn state (``interrupt_event``, ``client_is_speaking``, ``sentence_id``)
lives in ``PiguguTurnState`` and is read by the STT bridge for barge-in: when
the user talks over the assistant, the STT bridge broadcasts an
``InterruptionFrame``; this bridge aborts the running TTS and sends tts/abort.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger
from metrics.turn import TelemetryCollector
from pipecat.frames.frames import Frame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.pipecat.pigugu_serializer import (
    PiguguMessageFrame,
    PiguguOpusFrame,
    PiguguOutputMessageFrame,
    PiguguUserTurnFrame,
)
from voice.pipecat.state import PiguguTurnState
from voice.pipecat.telemetry import ensure_turn_context, telemetry_snapshot

TTS_FRAME_INTERVAL = 0.06  # 60 ms per Opus frame at 16 kHz
TTS_MAX_SEND_AHEAD = 1.2   # keep the device decode queue ~1.2s ahead
TTS_STREAM_WARMUP_FRAMES = 5


class PiguguTtsBridge(FrameProcessor):
    """Runs one reply per user turn and paces it out to the device."""

    def __init__(
        self,
        pig: Any,
        tts: Any,
        *,
        state: PiguguTurnState,
        session_id: str,
        user_id: str = "",
        persona_id: int = 1,
        on_start: Any = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._pig = pig
        self._tts = tts
        self._state = state
        self._session_id = session_id
        self._user_id = user_id or session_id
        self._persona_id = persona_id
        self._on_start = on_start
        self._tts_task: asyncio.Task | None = None
        self._tts_started = False
        self._audio_marked = False
        self._play_position = 0.0
        self._clock_start = time.monotonic()
        self._current_sentence_id = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, PiguguUserTurnFrame):
            await self._begin_turn(frame.text)
        elif isinstance(frame, InterruptionFrame):
            await self._abort()
        elif isinstance(frame, PiguguMessageFrame) and frame.message.get("type") == "abort":
            # Device-side abort (stop button / shutdown): always flush.
            await self._handle_device_abort()
        await self.push_frame(frame, direction)

    # ── turn lifecycle ────────────────────────────────────────────────

    async def _begin_turn(self, text: str):
        was_speaking = self._state.client_is_speaking
        if self._tts_task and not self._tts_task.done():
            # A reply is still in flight (e.g. barge-in already raced ahead).
            # Kill it and wait for its finally to run WHILE interrupt_event is
            # still set — otherwise the stale task could drain and emit a
            # tts/stop into the new turn.
            self._state.interrupt_event.set()
            self._tts_task.cancel()
            try:
                await self._tts_task
            except (asyncio.CancelledError, Exception):
                pass
            if was_speaking:
                # The user-turn interruption broadcast usually flushes the
                # device queue via _abort; if it was missed (e.g. server VAD
                # and STT barge-in both silent), flush here so the stale
                # device-queued audio cannot overlap the new reply.
                await self._push_message({"type": "tts", "state": "abort"})
                self._reset_clock()
        self._state.interrupt_event.clear()
        self._state.sentence_id += 1
        self._current_sentence_id = self._state.sentence_id
        self._reset_clock()
        self._tts_started = False
        self._audio_marked = False
        self._tts_task = self.create_task(self._run_tts(text))

    async def _run_tts(self, text: str):
        # This processor runs in its own task with an isolated contextvars copy —
        # re-bind the shared turn dict so our telemetry marks (and the LLM's,
        # inherited by the producer task) land in the SAME dict the observer
        # created. Capture the reference: a barge-in may start a new turn and
        # overwrite state.active_turn before we snapshot.
        ensure_turn_context(self._state)
        turn = self._state.active_turn
        # Consume the TurnStorage the observer built for this turn FIRST, so an
        # early failure (no TTS / lazy PigAgent down) still commits the user's
        # audio instead of it being overwritten by the next turn's observer.
        storage = self._state.turn_storage
        self._state.turn_storage = None
        if self._tts is None:
            logger.warning("[PiguguTtsBridge] no tts wired — dropping turn")
            self._finalize_failed_storage(storage, text, "no_tts")
            return
        # The PigAgent is lazy: it needs the user id + hw_id from the device
        # hello, which arrive after the session is built. Create it here on the
        # first turn (mirrors the old lazy create_pig_agent).
        if self._pig is None:
            self._pig = await self._ensure_pig()
            if self._pig is None:
                logger.warning("[PiguguTtsBridge] no PigAgent — dropping turn")
                self._finalize_failed_storage(storage, text, "agent_failed")
                return
        if storage is not None:
            storage.mark_stt_final(text)
        # Persist the user's utterance into the conversation context (the old
        # connection.py did this at STT final; agent.py delegates it to the
        # session layer).
        self._schedule_ctx("user", text)

        tts_pcm = bytearray()  # debug PCM → tts.wav
        holder: dict[str, str] = {"full": ""}
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def _producer() -> str:
            full = ""
            try:
                async for chunk in self._pig.generate_reply(
                    text,
                    persona_id=self._persona_id,
                    interrupt_event=self._state.interrupt_event,
                    session_id=self._session_id,
                ):
                    if self._state.interrupt_event.is_set():
                        break
                    if isinstance(chunk, str):
                        await text_queue.put(chunk)
                        full += chunk
            except Exception:
                logger.exception("[PiguguTtsBridge] LLM producer failed")
            finally:
                await text_queue.put(None)
                holder["full"] = full
            return full

        async def _iter_text() -> AsyncIterator[str]:
            while True:
                chunk = await text_queue.get()
                if chunk is None:
                    return
                yield chunk

        async def _send_start():
            payload: dict = {"type": "tts", "state": "start"}
            if self._current_sentence_id > 0:
                payload["sentence_id"] = self._current_sentence_id
            await self._push_message(payload)
            self._state.client_is_speaking = True
            self._state.current_sentence_id = self._current_sentence_id
            self._tts_started = True
            TelemetryCollector.mark("tts_start")
            if self._on_start:
                await self._on_start()

        async def _send_batch(frames: list[bytes], *, first: bool = False):
            if self._state.interrupt_event.is_set() or not frames:
                return
            if not self._tts_started:
                await _send_start()
            await self._pace(frames, mark_first=True)

        producer_task = self.create_task(_producer())
        tts_ok = False
        truncated_reason = ""
        cancelled = False
        try:
            pending: list[bytes] = []
            async for frames in self._tts.stream_audio(
                _iter_text(), self._state.interrupt_event, collect_pcm=tts_pcm
            ):
                if self._state.interrupt_event.is_set():
                    break
                if not self._tts_started:
                    # Warm-up gate: accumulate the first frames so a tiny first
                    # chunk + slow next chunk doesn't stutter playback start.
                    pending.extend(frames)
                    if len(pending) >= TTS_STREAM_WARMUP_FRAMES:
                        self._reset_clock()  # warm-up wait isn't "behind"
                        await _send_start()
                        await self._pace(pending, mark_first=True)
                        pending = []
                else:
                    await _send_batch(frames)
            # Stream ended while still warming up (very short reply): release
            # whatever audio accumulated.
            if not self._tts_started and pending and not self._state.interrupt_event.is_set():
                await _send_start()
                await self._pace(pending, mark_first=True)
        except RuntimeError:
            # Degraded mode: one whole-reply REST synthesis. Only when nothing
            # has been played yet — a mid-stream failure must not replay the
            # whole reply over already-played audio.
            if self._tts_started:
                logger.warning("[PiguguTtsBridge] WS stream died mid-turn — ending turn")
                truncated_reason = "stream_failed"
            elif not self._state.interrupt_event.is_set():
                logger.warning("[PiguguTtsBridge] WS stream failed — falling back to REST")
                full = await producer_task
                if full.strip() and self._tts:
                    try:
                        frames = await self._tts.synthesize(full.strip(), collect_pcm=tts_pcm)
                    except Exception:
                        logger.exception("[PiguguTtsBridge] REST fallback synthesize failed")
                    else:
                        if frames:
                            await _send_batch(frames, first=True)
        except asyncio.CancelledError:
            cancelled = True
            # Record WHY the turn was interrupted for the storage sidecar
            # (barge-in aborts set the interrupt event; a bare cancel is a
            # device abort / disconnect).
            truncated_reason = truncated_reason or (
                "barge_in" if self._state.interrupt_event.is_set() else "cancelled"
            )
            # Abort (barge-in / device abort / disconnect): stop the LLM too.
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        finally:
            if not self._state.interrupt_event.is_set():
                drained = await self._wait_playback_drain()
                if drained:
                    await self._push_message({"type": "tts", "state": "stop"})
                    tts_ok = True
                    TelemetryCollector.mark("tts_end")
                elif not cancelled:
                    truncated_reason = truncated_reason or "drain_timeout"
            elif not cancelled:
                truncated_reason = truncated_reason or "barge_in"
            if storage is not None:
                if not self._tts_started:
                    truncated_reason = truncated_reason or "no_tts_started"
                if tts_pcm:
                    storage.tts_pcm_buf.extend(tts_pcm)
                storage.mark_tts_complete(
                    holder["full"],
                    ok=tts_ok and not truncated_reason,
                    truncated_reason=truncated_reason,
                )
                storage.set_telemetry(
                    telemetry_snapshot(
                        device_playback_ms=self._state.device_playback_ms,
                        turn=turn,
                    )
                )
                # Commit is deferred to the next turn boundary (the observer
                # attaches this turn's listen.wav there). Signal finalization
                # so the observer never commits before the TTS mark lands.
                storage.mark_finalized()
            # Persist the assistant reply only when it completed naturally — an
            # interrupted reply is a partial sentence that would corrupt
            # multi-turn memory (old code persisted assistant only on
            # non-cancelled turns).
            if not self._state.interrupt_event.is_set() and not cancelled:
                self._schedule_ctx("assistant", holder["full"])
            self._state.client_is_speaking = False
            self._state.current_sentence_id = 0

    async def _abort(self):
        # UserTurnProcessor broadcasts an InterruptionFrame on EVERY turn start
        # (its barge-in mechanism). If nothing has been sent yet, that must not
        # produce a spurious tts/abort on the wire.
        had_started = self._state.client_is_speaking
        if self._tts_task and not self._tts_task.done():
            self._state.interrupt_event.set()
            self._tts_task.cancel()
            try:
                await self._tts_task
            except (asyncio.CancelledError, Exception):
                pass
        if had_started:
            await self._push_message({"type": "tts", "state": "abort"})
        self._state.client_is_speaking = False
        self._state.current_sentence_id = 0
        self._reset_clock()

    async def _handle_device_abort(self):
        """Device sent ``abort`` (stop button / shutdown): cancel the in-flight
        reply, always flush the device queue with tts/abort, and clear the
        interrupt so the next turn can run (mirrors the old _handle_abort)."""
        self._state.sentence_id += 1
        self._state.interrupt_event.set()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
            try:
                await self._tts_task
            except (asyncio.CancelledError, Exception):
                pass
        self._state.client_is_speaking = False
        self._state.current_sentence_id = 0
        await self._push_message({"type": "tts", "state": "abort"})
        self._reset_clock()
        self._state.interrupt_event.clear()

    # ── lazy PigAgent + context persistence ───────────────────────────

    async def _ensure_pig(self):
        from bootstrap.factory import create_pig_agent

        try:
            pig = await create_pig_agent(self._user_id, hw_id=self._state.hw_id)
            # The storage telemetry snapshot reads llm_model from the turn meta
            # (old connection.py set it right after creating the agent).
            TelemetryCollector.set_meta("llm_model", pig.model)
            TelemetryCollector.mark("agent_init")
            return pig
        except Exception:
            logger.exception("[PiguguTtsBridge] create_pig_agent failed")
            return None

    def _finalize_failed_storage(self, storage, text: str, reason: str) -> None:
        """Mark a turn that never reached the LLM/TTS, preserving the user
        utterance and recording why playback never happened. Commit is left
        to the observer at the next turn boundary."""
        if storage is None:
            return
        storage.mark_stt_final(text)
        storage.mark_tts_complete("", ok=False, truncated_reason=reason)
        storage.mark_finalized()

    def _schedule_ctx(self, role: str, content: str) -> None:
        """Fire-and-forget user/assistant turn persistence into ctx (the old
        connection.py _persist_turn). Only meaningful for the real PigAgent —
        test fakes without a ctx are skipped."""
        if not content or not content.strip():
            return
        pig = self._pig
        if pig is None or getattr(pig, "ctx", None) is None:
            return
        try:
            asyncio.ensure_future(self._ctx_add(role, content))
        except RuntimeError:
            # No running loop (shutdown) — dropping the turn text is acceptable.
            pass

    async def _ctx_add(self, role: str, content: str) -> None:
        try:
            await self._pig.ctx.add_turn(role=role, content=content)
        except Exception:
            logger.exception(f"[PiguguTtsBridge] ctx.add_turn({role}) failed")

    # ── pacing (virtual playback clock, ported from connection.py) ────

    async def _pace(self, frames: list[bytes], *, mark_first: bool = False, extra_break: Any = None):
        """Send frames paced by the playback clock, capped TTS_MAX_SEND_AHEAD
        ahead of real time so the device's decode queue stays prefilled. Waits
        are computed against the clock — no per-frame sleep drift. A negative
        lead (long stall) re-anchors the clock so the refill burst is capped.

        ``mark_first`` telemetry (tts_first_ready / agent_spk = E2E endpoint)
        is only for real turns — inject playback reuses this but must not
        overwrite the current turn's marks. ``extra_break`` is a predicate
        (inject: sentence id moved on) checked alongside the interrupt event.
        """
        for frame in frames:
            if self._state.interrupt_event.is_set() or (extra_break and extra_break()):
                return
            if mark_first and not self._audio_marked:
                self._audio_marked = True
                TelemetryCollector.mark("tts_first_ready")
                TelemetryCollector.mark("agent_spk")
            await self._push_opus(frame)
            self._play_position += TTS_FRAME_INTERVAL
            lead = self._play_position - (time.monotonic() - self._clock_start)
            if lead < 0:
                # Fell behind real time — re-anchor instead of bursting the
                # whole backlog out in one go.
                self._clock_start = time.monotonic() - self._play_position
                lead = 0.0
            if lead > TTS_MAX_SEND_AHEAD:
                await asyncio.sleep(lead - TTS_MAX_SEND_AHEAD)

    async def _wait_playback_drain(self, extra_break: Any = None) -> bool:
        """Wait until queued TTS audio has finished playing on the device, so
        tts/stop only fires when the device is actually done. Returns True if
        drained naturally, False if interrupted."""
        while True:
            if extra_break and extra_break():
                return False
            lead = self._play_position - (time.monotonic() - self._clock_start)
            if lead <= 0:
                return True
            try:
                await asyncio.wait_for(self._state.interrupt_event.wait(), timeout=lead)
                return False
            except asyncio.TimeoutError:
                pass

    def _reset_clock(self) -> None:
        """Re-anchor after an abort flushed the device queue — otherwise the
        stale lead would delay the next turn's first frames by up to
        TTS_MAX_SEND_AHEAD."""
        self._play_position = 0.0
        self._clock_start = time.monotonic()

    # ── inject (roast etc.): external text, no turn, no storage ──────

    async def inject_text(self, text: str):
        """Play injected text over the current reply (interrupts it). Mirrors
        the old inject_roast path: it does NOT build a turn or touch
        TurnStorage — the interrupted turn commits itself via its own finally."""
        if self._tts is None:
            return
        was_speaking = self._state.client_is_speaking
        if self._tts_task and not self._tts_task.done():
            self._state.interrupt_event.set()
            self._tts_task.cancel()
            try:
                await self._tts_task
            except (asyncio.CancelledError, Exception):
                pass
        self._state.interrupt_event.clear()
        if was_speaking:
            # The device may still hold queued audio — flush it so the
            # inject starts promptly.
            self._reset_clock()
            await self._push_message({"type": "tts", "state": "abort"})
        text = (text or "").strip()
        if not text:
            return
        # A new turn increments state.sentence_id — if that happens mid-inject
        # (user barge-in landing as a fresh turn), stop feeding frames and skip
        # the drain/stop so the new turn owns the wire.
        inject_sid = self._state.sentence_id
        stale = lambda: inject_sid != self._state.sentence_id
        tts_pcm = bytearray()
        try:
            frames = await self._tts.synthesize(text, collect_pcm=tts_pcm)
        except Exception:
            logger.exception("[PiguguTtsBridge] inject synthesize failed")
            return
        if not frames:
            return
        await self._push_message({"type": "tts", "state": "start"})
        self._state.client_is_speaking = True
        await self._pace(frames, mark_first=False, extra_break=stale)
        if (
            not stale()
            and not self._state.interrupt_event.is_set()
            and await self._wait_playback_drain(extra_break=stale)
        ):
            await self._push_message({"type": "tts", "state": "stop"})
        self._state.client_is_speaking = False

    # ── wire helpers ──────────────────────────────────────────────────

    async def _push_opus(self, audio: bytes):
        await self.push_frame(PiguguOpusFrame(audio=audio))

    async def _push_message(self, msg: dict):
        await self.push_frame(PiguguOutputMessageFrame(message=msg))
