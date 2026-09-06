"""Per-device pigugu session: hello handshake + Pipecat pipeline.

One ``PiguguSession`` is created per device WebSocket. It owns the raw
connection, builds a Pipecat pipeline around it (custom input/output
transports + the middle processors), and runs a ``PipelineWorker`` until the
connection ends (the input transport's read loop fires the on-disconnect
callback, which stops the worker gracefully).
"""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from typing import Any

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_transport import TransportParams
from pipecat.pipeline.worker import PipelineWorker
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy, SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.asyncio.task_manager import TaskManager
from pipecat.workers.base_worker import WorkerParams

from voice.pipecat.agent_gateway import PiguguAgentGateway
from voice.pipecat.echo_processor import PiguguEchoProcessor
from voice.pipecat.pigugu_serializer import SAMPLE_RATE, PiguguFrameSerializer
from voice.pipecat.state import PiguguTurnState
from voice.pipecat.stt_bridge import PiguguSttBridge
from voice.pipecat.transports import PiguguInputTransport, PiguguOutputTransport
from voice.pipecat.tts_bridge import PiguguTtsBridge
from voice.pipecat.turn_storage_observer import PiguguTurnStorageObserver
from voice.pipecat.vad_bridge import PiguguVadBridge

# idle#1 (silence): bot finishes a reply, then this long with no new user
# turn → close the WS. Driven by the UserIdleController that UserTurnProcessor
# embeds (see _default_processors). Default 30s = the PRD follow-up window.
# idle#2 (device lost): a session that stops receiving audio frames for this
# long is judged dead (power off / network drop — the device never sent a clean
# close). Realtime devices stream mic audio whenever connected, so an idle WS
# with no frames only happens on abnormal loss. The PipelineWorker's
# on_idle_timeout fires here (idle_timeout_frames=(InputAudioRawFrame,)).
# Shrunk from the old 120s backstop to a ~30s reap.
#
# The prod values are injected by deploy.yml into the agent ConfigMap (see
# k8s/agent.yaml). Tolerate a non-numeric value (e.g. a stray placeholder from
# an out-of-band `kubectl apply`, or a mistyped Actions variable) instead of
# crashing the pod at import: warn and fall back to the 30s default.


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        logger.warning(f"[PiguguSession] {name}={raw!r} not a number — using {default}")
        return default
    if not math.isfinite(val):
        # nan/inf parse but would poison asyncio.wait_for / idle timeouts
        # (nan raises at runtime); treat them as misconfig like any bad value.
        logger.warning(f"[PiguguSession] {name}={raw!r} not finite — using {default}")
        return default
    return val


VOICE_IDLE_SILENCE_SECS = _env_float("VOICE_IDLE_SILENCE_SECS", 30.0)
VOICE_LOST_TIMEOUT_SECS = _env_float("VOICE_LOST_TIMEOUT_SECS", 30.0)


def _stop_strategies(stt):
    """Pick the user turn-stop strategy for the active STT provider.

    "vad" providers (Deepgram): speech-timeout — an inactivity fallback ends
    the turn when no new transcript arrives within 0.6s (their utterance_end
    is slow, so the fallback carries the turn-end).
    "external" providers (AssemblyAI): the model's semantic endpointing drives
    turn-end via ProposedUserStoppedSpeakingFrame — no inactivity fallback, so
    a mid-sentence pause never splits a turn.
    """
    if getattr(stt, "turn_end_signal", "vad") == "external":
        # wait_for_transcript=False: the provider's semantic endpointing is the
        # authoritative stop signal — an end_of_turn with an empty transcript
        # must still end the turn (otherwise it hangs until the watchdog and the
        # user's next utterance merges into the same turn). The 0.2s timeout
        # batches the final transcript that precedes the stop signal anyway.
        return [ExternalUserTurnStopStrategy(timeout=0.2, wait_for_transcript=False)]
    return [SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)]


def _default_processors(
    *,
    vad: Any | None = None,
    stt: Any | None = None,
    pig: Any | None = None,
    tts: Any | None = None,
    stt_context_loader: Any = None,
    session_id: str = "",
    client_id: str = "",
    user_id: str = "",
    persona_id: int = 1,
    user_idle_timeout: float = 0,
) -> tuple[
    list[FrameProcessor],
    PiguguTurnState,
    PiguguTtsBridge | None,
    PiguguTurnStorageObserver | None,
    UserTurnProcessor | None,
]:
    state = PiguguTurnState()
    if vad is None and stt is None and pig is None and tts is None:
        # M1: loopback echo until the real chain is wired up.
        return [PiguguEchoProcessor()], state, None, None, None
    if tts is None:
        # M2: no TTS yet — stop at transcript accumulation.
        turn_processor = UserTurnProcessor(
            user_turn_strategies=UserTurnStrategies(
                # Official pipecat barge-in pattern (turn-management
                # interruption example): MinWords gates turn start by word
                # count, and only while the bot is speaking — a short
                # affirmation can't interrupt, a real interruption needs
                # min_words. Turn start broadcasts the interruption
                # (enable_interruptions defaults True): a user turn starting
                # over the bot IS the barge-in.
                start=[MinWordsUserTurnStartStrategy(min_words=3)],
                stop=_stop_strategies(stt),
            ),
            user_idle_timeout=user_idle_timeout,
        )
        chain = [
            PiguguVadBridge(vad, state=state),
            PiguguSttBridge(stt, state=state, context_loader=stt_context_loader),
            turn_processor,
            # No tts_bridge in this chain: a dispatched turn would never reach
            # a tts/stop·abort, so turn/start (a promise of a release) must not
            # be sent — it would leave the device's idle pause armed forever.
            PiguguAgentGateway(state=state, emit_turn_start=False),
        ]
        return chain, state, None, None, turn_processor
    # M3/M4: full loop — turn transcript → LLM → Cartesia → paced Opus, with
    # per-turn TurnStorage built by the observer and committed by the TTS bridge.
    # ``pig`` may be None: the TTS bridge creates it lazily on the first turn
    # (needs the user id + hw_id from the device hello).
    tts_bridge = PiguguTtsBridge(
        pig,
        tts,
        state=state,
        session_id=session_id,
        user_id=user_id,
        persona_id=persona_id,
    )
    vad_bridge = PiguguVadBridge(vad, state=state)
    # The observer needs the VAD BRIDGE (not the provider): silero's is_vad
    # stores per-connection voice-chunk flags on the conn.
    observer = PiguguTurnStorageObserver(
        vad_bridge,
        state,
        session_id=session_id,
        client_id=client_id,
        user_id=user_id,
        persona_id=persona_id,
    )
    stt_bridge = PiguguSttBridge(stt, state=state, context_loader=stt_context_loader)
    # Context-aware STT (supports_context): route each completed agent reply
    # into the decoder as conversation context (e.g. AssemblyAI agent_context).
    # Providers without it (Deepgram) keep a fully inert path.
    if stt is not None and getattr(stt, "supports_context", False):
        tts_bridge.set_stt_context_cb(stt_bridge.push_context)
    turn_processor = UserTurnProcessor(
        user_turn_strategies=UserTurnStrategies(
            # Official pipecat barge-in pattern (see the M2 branch comment).
            start=[MinWordsUserTurnStartStrategy(min_words=3)],
            stop=_stop_strategies(stt),
        ),
        user_idle_timeout=user_idle_timeout,
    )
    chain = [
        vad_bridge,
        stt_bridge,
        turn_processor,
        observer,
        PiguguAgentGateway(state=state),
        tts_bridge,
    ]
    return chain, state, tts_bridge, observer, turn_processor


class PiguguSession:
    """Runs one device's voice pipeline on a Pipecat PipelineWorker."""

    def __init__(
        self,
        websocket: Any,
        *,
        client_id: str = "",
        user_id: str = "",
        session_id: str | None = None,
        processors: list[FrameProcessor] | None = None,
        vad: Any | None = None,
        stt: Any | None = None,
        pig: Any | None = None,
        tts: Any | None = None,
        stt_context_loader: Any = None,
        persona_id: int = 1,
    ):
        self._ws = websocket
        self.client_id = client_id
        self.user_id = user_id or client_id
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self._serializer = PiguguFrameSerializer()
        self.state = PiguguTurnState()
        self._stt = stt
        self._tts_bridge: PiguguTtsBridge | None = None
        self._observer: PiguguTurnStorageObserver | None = None
        self._inject_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._inject_task: asyncio.Task | None = None
        self._user_turn: UserTurnProcessor | None = None
        if processors is not None:
            self._processors = processors
        else:
            (
                self._processors,
                self.state,
                self._tts_bridge,
                self._observer,
                self._user_turn,
            ) = _default_processors(
                vad=vad,
                stt=stt,
                pig=pig,
                tts=tts,
                stt_context_loader=stt_context_loader,
                session_id=self.session_id,
                client_id=client_id,
                user_id=self.user_id,
                persona_id=persona_id,
                user_idle_timeout=VOICE_IDLE_SILENCE_SECS,
            )
        self._worker: PipelineWorker | None = None
        self._stop_requested = False
        self._idle_closing = False

    @property
    def serializer(self) -> PiguguFrameSerializer:
        return self._serializer

    async def _on_disconnect(self):
        if self._worker:
            await self._worker.stop_when_done()
        else:
            self._stop_requested = True

    # ── idle close (idle#1 silence / idle#2 device lost) ─────────────

    async def _close_for_idle(self, reason: str) -> None:
        """Server-initiated idle close: log the reason, close the device WS,
        and let the input transport's read loop end (its finally fires
        ``_on_disconnect`` → graceful worker stop → storage finalize)."""
        if self._idle_closing:
            return
        self._idle_closing = True
        logger.info(f"[PiguguSession] {self.session_id} idle close: {reason}")
        try:
            await self._ws.close()
        except Exception:
            logger.debug(f"[PiguguSession] {self.session_id} idle close ws error", exc_info=True)
        # The read-loop finally may have raced us; make sure the worker stops
        # even if the ws was already gone.
        await self._on_disconnect()

    async def _on_user_turn_idle(self, *_args: Any) -> None:
        # idle#1: bot finished a reply and the user stayed silent for
        # VOICE_IDLE_SILENCE_SECS with no new turn. Fired by the
        # UserIdleController embedded in our UserTurnProcessor.
        await self._close_for_idle("idle_no_speech")

    async def _on_pipeline_idle_timeout(self, *_args: Any) -> None:
        # idle#2: no audio frame for VOICE_LOST_TIMEOUT_SECS — the device is
        # gone without a clean close (power off / network drop). Fired by the
        # PipelineWorker on_idle_timeout.
        await self._close_for_idle("lost")

    # ── inject (roast etc.), from the REST side via send_inject ───────

    async def inject(self, msg: dict) -> None:
        await self._inject_queue.put(msg)

    async def _inject_consumer(self) -> None:
        while True:
            try:
                msg = await asyncio.wait_for(self._inject_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            if self._tts_bridge is None:
                continue
            text = msg.get("text", msg.get("content", msg.get("prompt", "")))
            logger.info(f"[PiguguSession] inject: {msg.get('type', '?')}")
            await self._tts_bridge.inject_text(text)

    # ── storage cleanup on disconnect ─────────────────────────────────

    async def _finalize_pending_storage(self):
        """If a turn ended but its storage was never closed at a turn boundary
        (e.g. worker torn down before the next turn), close it now with the
        trailing listen audio so the user's audio + reply are not lost."""
        if self._observer is not None:
            await self._observer.finalize_session()

    async def run(self):
        # Server-side accept anchor for the per-session connect_pre_roll metric.
        self.state.accept_pc = time.perf_counter()
        params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=SAMPLE_RATE,
            audio_out_channels=1,
        )
        input_transport = PiguguInputTransport(
            self._ws,
            self._serializer,
            params,
            session_id=self.session_id,
            state=self.state,
            on_disconnect=self._on_disconnect,
        )
        output_transport = PiguguOutputTransport(self._ws, self._serializer, params)
        pipeline = Pipeline([input_transport, *self._processors, output_transport])
        self._worker = PipelineWorker(
            pipeline,
            enable_rtvi=False,
            enable_turn_tracking=False,
            # Match the old no-voice close: drop idle connections (~120s of
            # no frames) instead of leaking sessions. The custom transports
            # and bridges never emit pipecat's default idle-reset frames
            # (BotSpeakingFrame / UserSpeakingFrame), so without an explicit
            # frame set every session was killed exactly 120s after connect,
            # mid-conversation. The device mic stream is the authoritative
            # liveness signal: it flows in every conversation state (including
            # TTS playback) and stops only when the device returns to standby.
            idle_timeout_frames=(InputAudioRawFrame,),
            idle_timeout_secs=VOICE_LOST_TIMEOUT_SECS,
            # idle#2: on_idle_timeout fires after no audio frame for
            # VOICE_LOST_TIMEOUT_SECS (device lost). We own the teardown in
            # _on_pipeline_idle_timeout (close WS → read loop ends → graceful
            # stop → storage finalize), so disable the worker's own auto-cancel
            # — two concurrent teardown paths would race.
            cancel_on_idle_timeout=False,
            name=f"pigugu-{self.session_id}",
        )
        self._worker.event_handler("on_idle_timeout")(self._on_pipeline_idle_timeout)
        # idle#1: UserTurnProcessor embeds UserIdleController; arm it with the
        # silence window and close the WS when it fires.
        if self._user_turn is not None:
            self._user_turn.event_handler("on_user_turn_idle")(self._on_user_turn_idle)
        self._inject_task = asyncio.ensure_future(self._inject_consumer())
        logger.info(f"[PiguguSession] {self.session_id} pipeline running")
        if self._stop_requested:
            await self._worker.stop_when_done()
        try:
            await self._worker.run(WorkerParams(task_manager=TaskManager()))
        finally:
            await self._finalize_pending_storage()
            if self._inject_task:
                self._inject_task.cancel()
                try:
                    await self._inject_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._cleanup()
            logger.info(f"[PiguguSession] {self.session_id} pipeline ended")

    def _submit_session_metrics(self) -> None:
        """Emit one ``metrics.session`` row (connect_pre_roll) for this
        connection. Fire-and-forget; a no-op when the exporter is disabled."""
        try:
            st = self.state
            if not (st.accept_pc and (st.hello_pc or st.first_audio_pc)):
                return
            from metrics.exporter import enqueue
            from metrics.scope import SessionScope

            scope = SessionScope(
                user_id=self.user_id,
                device_id=self.client_id,
                session_id=self.session_id,
            )
            scope.set_meta("accept_pc", st.accept_pc)
            if st.hello_pc:
                scope.set_meta("hello_pc", st.hello_pc)
            if st.first_audio_pc:
                scope.set_meta("first_audio_pc", st.first_audio_pc)
            scope.finish()
            enqueue(scope)
        except Exception:
            logger.debug(f"[PiguguSession] {self.session_id} session metrics failed")

    async def _cleanup(self) -> None:
        """Release per-connection resources (old connection.py _cleanup):
        the Deepgram socket, the conversation-context flush, and the final
        turn's telemetry (which otherwise never gets flushed/logged)."""
        if self.state.active_turn is not None:
            try:
                from metrics.turn import _current_var as _turn_var
                from metrics.turn import TelemetryCollector

                _turn_var.set(self.state.active_turn)
                TelemetryCollector._flush_turn()
            except Exception:
                logger.debug(f"[PiguguSession] {self.session_id} telemetry flush failed")
        self._submit_session_metrics()
        if self._stt is not None:
            try:
                await self._stt.close_audio_channels()
            except Exception:
                logger.debug(f"[PiguguSession] {self.session_id} stt close failed")
        pig = self._tts_bridge._pig if self._tts_bridge else None
        if pig is not None and getattr(pig, "ctx", None) is not None:
            try:
                await pig.ctx.flush()
            except Exception:
                logger.debug(f"[PiguguSession] {self.session_id} ctx flush failed")
