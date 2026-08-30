"""Per-device pigugu session: hello handshake + Pipecat pipeline.

One ``PiguguSession`` is created per device WebSocket. It owns the raw
connection, builds a Pipecat pipeline around it (custom input/output
transports + the middle processors), and runs a ``PipelineWorker`` until the
connection ends (the input transport's read loop fires the on-disconnect
callback, which stops the worker gracefully).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_transport import TransportParams
from pipecat.pipeline.worker import PipelineWorker
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
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


def _default_processors(
    *,
    vad: Any | None = None,
    stt: Any | None = None,
    pig: Any | None = None,
    tts: Any | None = None,
    session_id: str = "",
    client_id: str = "",
    user_id: str = "",
    persona_id: int = 1,
) -> tuple[list[FrameProcessor], PiguguTurnState, PiguguTtsBridge | None, PiguguTurnStorageObserver | None]:
    state = PiguguTurnState()
    if vad is None and stt is None and pig is None and tts is None:
        # M1: loopback echo until the real chain is wired up.
        return [PiguguEchoProcessor()], state, None, None
    if tts is None:
        # M2: no TTS yet — stop at transcript accumulation.
        chain = [
            PiguguVadBridge(vad, state=state),
            PiguguSttBridge(stt, state=state),
            UserTurnProcessor(
                user_turn_strategies=UserTurnStrategies(
                    start=[VADUserTurnStartStrategy()],
                    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
                ),
            ),
            PiguguAgentGateway(),
        ]
        return chain, state, None, None
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
    chain = [
        vad_bridge,
        PiguguSttBridge(stt, state=state),
        UserTurnProcessor(
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy()],
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
            ),
        ),
        observer,
        PiguguAgentGateway(),
        tts_bridge,
    ]
    return chain, state, tts_bridge, observer


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
        if processors is not None:
            self._processors = processors
        else:
            self._processors, self.state, self._tts_bridge, self._observer = _default_processors(
                vad=vad,
                stt=stt,
                pig=pig,
                tts=tts,
                session_id=self.session_id,
                client_id=client_id,
                user_id=self.user_id,
                persona_id=persona_id,
            )
        self._worker: PipelineWorker | None = None
        self._stop_requested = False

    @property
    def serializer(self) -> PiguguFrameSerializer:
        return self._serializer

    async def _on_disconnect(self):
        if self._worker:
            await self._worker.stop_when_done()
        else:
            self._stop_requested = True

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
            # no frames) instead of leaking sessions.
            idle_timeout_secs=120,
            name=f"pigugu-{self.session_id}",
        )
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
