"""Connection handler — one instance per device WebSocket.

Replaces the old monolithic ``ws/handler.py``.  Uses pluggable providers
(VAD / STT / TTS / LLM) and follows the official xiaozhi-esp32-server
architecture while preserving PigAgent-specific integrations (metrics,
roast inject, persistence).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from typing import Any

from loguru import logger
from starlette.websockets import WebSocket, WebSocketDisconnect

from bootstrap.factory import create_pig_agent, get_pg_pool, get_redis
from metrics.session import ColdStartMetrics
from metrics.turn import TelemetryCollector
from providers.base import STTProvider, TTSProvider, VADProvider


# ── Opus helpers (follows official xiaozhi-esp32-server pattern) ───────

def _make_opus_decoder(sample_rate: int = 16000, channels: int = 1) -> Any:
    """Create an Opus decoder. Returns None if opuslib not installed."""
    try:
        import opuslib  # pyright: ignore[reportMissingImports]
        return opuslib.Decoder(sample_rate, channels)
    except ImportError:
        return None


def _opus_decode(data: bytes, decoder: Any) -> bytes | None:
    """Decode an Opus packet → PCM. Returns None on failure.

    Mirrors the official ``_decode_opus_packet`` from xiaozhi-esp32-server.
    For browser testing (raw PCM), returns data directly if >200 bytes.
    """
    if not data:
        return None
    if decoder is None:
        return data  # opuslib not installed
    try:
        # 60ms frame at 16kHz = 960 samples
        return decoder.decode(data, 960)
    except Exception:
        # Fallback: raw PCM from browser test is much larger than Opus frames
        if len(data) > 200:
            return data
        return None


def _opus_encode_chunks(
    pcm: bytes, sample_rate: int = 16000, channels: int = 1, frame_duration_ms: int = 60
) -> list[bytes]:
    """Encode raw PCM s16le → list of Opus frames."""
    try:
        import opuslib  # pyright: ignore[reportMissingImports]

        encoder = opuslib.Encoder(sample_rate, channels, "voip")
        frames: list[bytes] = []
        frame_samples = frame_duration_ms * sample_rate // 1000
        frame_bytes = frame_samples * channels * 2
        pos = 0
        while pos + frame_bytes <= len(pcm):
            frame = pcm[pos : pos + frame_bytes]
            try:
                encoded = encoder.encode(bytes(frame), frame_samples)
                frames.append(encoded)
            except Exception:
                pass
            pos += frame_bytes
        return frames
    except ImportError:
        logger.warning("[Opus] opuslib not available for encode")
        return [pcm]


# ── ConnectionHandler ─────────────────────────────────────────────────

class ConnectionHandler:
    """Per-device xiaozhi WebSocket connection handler.

    Manages the full voice-assistant lifecycle for a single ESP32 device:
    hello handshake → VAD-driven audio capture → STT → LLM → TTS → Opus back.

    Provider slots (set by the server / factory):
    - ``self.vad`` : VADProvider   (shared)
    - ``self.stt`` : STTProvider   (shared)
    - ``self.tts`` : TTSProvider   (shared)
    - ``self.llm`` : LLMProvider   (shared, but not used directly — we call
      ``create_pig_agent()`` for the PigAgent async interface)
    """

    # ── Public slots (set externally) ─────────────────────────────────
    vad: VADProvider | None = None
    stt: STTProvider | None = None
    tts: TTSProvider | None = None

    def __init__(self, ws: WebSocket, client_id: str = ""):
        self.ws = ws
        self.client_id = client_id
        self.session_id = str(uuid.uuid4())[:8]

        # ---- Identity (populated during hello) ----
        self._user_id: str = ""
        self._hw_id: str = ""
        self._persona_id: int = 1
        self._raw_pcm: bool = False  # set by hello: format='pcm' for browser
        self._pig: Any = None  # PigAgent (lazy-created)

        # ---- Listen-mode state ----
        self._listening = False
        self._audio_frames: list[bytes] = []
        self._pre_buffer: list[bytes] = []  # frames before detect

        # ---- VAD state (per the official pattern) ----
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window: deque[bool] = deque(maxlen=5)
        self.client_listen_mode = "auto"
        self.last_is_voice = False
        self.vad_last_voice_time: float = 0.0
        # VAD-internal state (set dynamically by SileroVAD provider)
        self._vad_pcm_buffer: bytearray = bytearray()
        self._voice_window: deque[bool] = deque(maxlen=5)

        # ---- Turn / interrupt tracking ----
        self._interrupt_event = asyncio.Event()
        self._sentence_id: int = 0
        self._tts_task: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        self._silence_timer: asyncio.Task | None = None

        # ---- Silence watchdog config ----
        self._watchdog_started_at: float = 0.0
        self._max_listen_seconds: float = float(
            os.getenv("MAX_LISTEN_SECONDS", "15.0")
        )

        # ---- Opus decoder (reused across frames for state continuity) ----
        self._opus_decoder = _make_opus_decoder(16000, 1)

        # ---- HTTP session (reused for latency) ----
        self._http: Any = None  # aiohttp.ClientSession

    # ── Main dispatch ─────────────────────────────────────────────────

    async def run(self) -> None:
        """Accept the WebSocket and dispatch messages until disconnect."""
        await self.ws.accept()
        ColdStartMetrics.start(session_id=self.session_id, room_name=self.client_id)
        ColdStartMetrics.mark("entry")
        logger.info(
            f"[Voice] Connected client={self.client_id} session={self.session_id}"
        )

        try:
            while True:
                msg = await self.ws.receive()
                if "text" in msg:
                    await self._dispatch_json(json.loads(msg["text"]))
                elif "bytes" in msg:
                    await self._handle_binary(msg["bytes"])
        except WebSocketDisconnect:
            logger.info(f"[Voice] Disconnected session={self.session_id}")
        except Exception as exc:
            logger.error(f"[Voice] Error: {exc}")
        finally:
            await self._cleanup()

    async def _dispatch_json(self, data: dict) -> None:
        msg_type = data.get("type", "")
        if msg_type == "hello":
            await self._handle_hello(data)
        elif msg_type == "listen":
            await self._handle_listen(data)
        elif msg_type == "abort":
            await self._handle_abort(data)
        else:
            logger.debug(f"[Voice] Unhandled message type: {msg_type}")

    # ── Hello ─────────────────────────────────────────────────────────

    async def _handle_hello(self, data: dict) -> None:
        transport = data.get("transport", "")
        if transport != "websocket":
            await self.ws.send_json(
                {"type": "hello", "transport": transport, "session_id": self.session_id}
            )
            return

        self._persona_id = int(data.get("persona_id", 1))
        self._hw_id = str(data.get("hw_id", ""))
        audio_params = data.get("audio_params", {})
        self._raw_pcm = audio_params.get("format", "opus") == "pcm"

        logger.info(
            f"[Voice] Hello client={self.client_id} "
            f"persona={self._persona_id} hw_id={self._hw_id} "
            f"version={data.get('version')} "
            f"sample_rate={audio_params.get('sample_rate')} "
            f"frame_duration={audio_params.get('frame_duration')}"
        )

        await self.ws.send_json(
            {
                "type": "hello",
                "transport": "websocket",
                "session_id": self.session_id,
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }
        )

    # ── Listen state machine ──────────────────────────────────────────

    async def _handle_listen(self, data: dict) -> None:
        state = data.get("state", "")

        if state == "detect":
            text = data.get("text", "")
            logger.info(
                f"[Voice] Wake word '{text}' client={self.client_id}"
            )

            # Abort any in-flight turn AND its TTS task
            if self._turn_task and not self._turn_task.done():
                self._sentence_id += 1
                self._interrupt_event.set()
                self._turn_task.cancel()
                logger.info("[Voice] Aborted in-flight turn for new detect")
            if self._tts_task and not self._tts_task.done():
                self._tts_task.cancel()

            self._interrupt_event.clear()
            self._listening = True

            # Flush pre-buffer (wake word frames arrived before detect)
            self._audio_frames = list(self._pre_buffer)
            self._pre_buffer.clear()

            # Init VAD state for this turn (both handler + VAD-internal attrs)
            self.client_audio_buffer.clear()
            self.client_have_voice = False
            self.client_voice_stop = False
            self.client_voice_window.clear()
            self.last_is_voice = False
            self.vad_last_voice_time = 0.0
            # Also clear the VAD's internal buffers
            self._vad_pcm_buffer.clear()
            self._voice_window.clear()

            self._start_silence_watchdog()
            logger.info(
                f"[Voice] Listening start (from detect) frames={len(self._audio_frames)}"
            )

        elif state == "start":
            logger.info(
                f"[Voice] Firmware start frames={len(self._audio_frames)}"
            )
            self._start_silence_watchdog()

        elif state == "stop":
            self._listening = False
            self._cancel_silence_watchdog()
            n = len(self._audio_frames)
            logger.info(f"[Voice] Listening stop frames={n}")
            if n > 0:
                self._turn_task = asyncio.create_task(self._process_turn())

    # ── Abort ─────────────────────────────────────────────────────────

    async def _handle_abort(self, data: dict) -> None:
        reason = data.get("reason", "unknown")
        logger.info(
            f"[Voice] Abort reason={reason} session={self.session_id}"
        )
        self._sentence_id += 1
        self._interrupt_event.set()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()

    # ── Binary audio (Opus frames from ESP32) ─────────────────────────

    async def _handle_binary(self, data: bytes) -> None:
        if not self._listening:
            # Buffer pre-detect frames (wake word audio)
            self._pre_buffer.append(data)
            if len(self._pre_buffer) > 200:
                self._pre_buffer.pop(0)
            return

        self._audio_frames.append(data)

        # Decode to PCM for VAD
        pcm = _opus_decode(data, self._opus_decoder)
        if not pcm or len(pcm) < 2:
            return
        if len(pcm) % 2 != 0:
            pcm = pcm[:-1]

        # Run official-pattern VAD on every frame
        if self.vad is not None:
            have_voice = self.vad.is_vad(self, pcm)

            # Voice → silence transition with sustained silence check
            if not have_voice and self.client_voice_stop:
                logger.info(
                    f"[Voice] VAD voice stop frames={len(self._audio_frames)}"
                )
                self._listening = False
                self._cancel_silence_watchdog()
                # Cancel any in-flight turn before creating a new one
                if self._turn_task and not self._turn_task.done():
                    self._sentence_id += 1
                    self._interrupt_event.set()
                    self._turn_task.cancel()
                self._turn_task = asyncio.create_task(self._process_turn())
                return

    # ── Silence watchdog (hard cap) ───────────────────────────────────

    def _start_silence_watchdog(self) -> None:
        if self._silence_timer and not self._silence_timer.done():
            self._silence_timer.cancel()
        self._watchdog_started_at = time.time()
        self._silence_timer = asyncio.create_task(self._silence_watchdog())

    async def _silence_watchdog(self) -> None:
        while self._listening:
            elapsed = time.time() - self._watchdog_started_at
            if elapsed > self._max_listen_seconds:
                logger.warning(
                    f"[Voice] Max listen duration ({self._max_listen_seconds}s) "
                    f"reached — auto-stopping frames={len(self._audio_frames)}"
                )
                self._listening = False
                if len(self._audio_frames) > 0:
                    self._turn_task = asyncio.create_task(self._process_turn())
                return
            await asyncio.sleep(1.0)

    def _cancel_silence_watchdog(self) -> None:
        if self._silence_timer and not self._silence_timer.done():
            self._silence_timer.cancel()
        self._silence_timer = None

    # ── Turn processing pipeline ──────────────────────────────────────

    async def _process_turn(self) -> None:
        """Opus → PCM → STT → PigAgent → TTS → Opus → WebSocket."""
        frames = list(self._audio_frames)
        self._audio_frames.clear()
        if not frames:
            return

        sentence_id = self._sentence_id
        self._sentence_id += 1

        TelemetryCollector.start_turn(
            user_id=self._user_id or self.client_id,
            persona_id=self._persona_id,
        )
        TelemetryCollector.mark("vad_end")

        try:
            # 1. Opus → PCM
            decoder = _make_opus_decoder(16000, 1)
            pcm_frames = []
            for f in frames:
                pcm_f = _opus_decode(f, decoder)
                if pcm_f:
                    pcm_frames.append(pcm_f)
            pcm = b"".join(pcm_frames)
            logger.warning(
                f"[Voice] Opus decode: {len(frames)} frames → {len(pcm)} bytes PCM "
                f"(first_sizes={[len(f) for f in frames[:3]]})"
            )
            if len(pcm) < 1600:
                logger.debug(f"[Voice] Audio too short: {len(pcm)} bytes")
                return

            # 2. STT
            if self.stt is None:
                logger.error("[Voice] No STT provider")
                return
            stt_text = await self.stt.transcribe(pcm)
            if not stt_text.strip():
                # Show PCM stats to diagnose browser Opus encoding issues
                import struct
                samples = struct.unpack('<10h', pcm[:20]) if len(pcm) >= 20 else []
                logger.warning(
                    f"[Voice] STT empty: {len(frames)} frames, "
                    f"{len(pcm)} bytes PCM, first_10_samples={samples}"
                )
                return

            TelemetryCollector.mark("stt_final")
            logger.info(f"[Voice] STT: '{stt_text[:120]}'")

            await self.ws.send_json(
                {
                    "session_id": self.session_id,
                    "type": "stt",
                    "text": stt_text.strip(),
                }
            )

            # 3. Persist user message
            if self._pig and self._pig.ctx:
                asyncio.create_task(self._persist_turn("user", stt_text.strip()))

            # 4. Create PigAgent (once per connection)
            self._user_id = self._user_id or self.client_id
            if self._pig is None:
                self._pig = await create_pig_agent(
                    self._user_id, hw_id=self._hw_id
                )
                ColdStartMetrics.set_meta("user_id", self._user_id)
                ColdStartMetrics.set_meta("persona_id", self._persona_id)
                ColdStartMetrics.set_meta("llm_model", self._pig.model)
                TelemetryCollector.set_meta("llm_model", self._pig.model)
                ColdStartMetrics.mark("agent_created")
                ColdStartMetrics.flush()

            TelemetryCollector.mark("agent_req")

            # 5. LLM → TTS streaming
            self._tts_task = asyncio.create_task(
                self._tts_producer_consumer(stt_text.strip(), sentence_id)
            )
            await self._tts_task

        except asyncio.CancelledError:
            logger.info(f"[Voice] Turn cancelled sid={sentence_id}")
        except Exception:
            logger.exception(f"[Voice] Turn processing failed sid={sentence_id}")
        finally:
            TelemetryCollector.finish_turn()

    # ── LLM → TTS producer-consumer ───────────────────────────────────

    async def _tts_producer_consumer(self, stt_text: str, sentence_id: int) -> None:
        """Run LLM producer and TTS consumer in parallel."""
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        pig = self._pig
        assert pig is not None, "PigAgent not initialised"

        async def _producer() -> str:
            full = ""
            try:
                async for chunk in pig.generate_reply(
                    stt_text,
                    persona_id=self._persona_id,
                    interrupt_event=self._interrupt_event,
                    session_id=self.session_id,
                ):
                    if self._interrupt_event.is_set():
                        break
                    if isinstance(chunk, str):
                        await text_queue.put(chunk)
                        full += chunk
            except Exception:
                logger.exception("[Voice] LLM producer failed")
            finally:
                await text_queue.put(None)  # EOF
            return full

        async def _consumer() -> None:
            if self.tts is None:
                # Drain the queue
                while True:
                    chunk = await text_queue.get()
                    if chunk is None:
                        break
                return

            text_buffer = ""
            full_spoken = ""
            tts_started = False
            first_tts = True

            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(text_queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        if text_buffer:
                            if not tts_started:
                                await self.ws.send_json(
                                    {
                                        "session_id": self.session_id,
                                        "type": "tts",
                                        "state": "start",
                                    }
                                )
                                TelemetryCollector.mark("tts_start")
                                tts_started = True
                            frames = await self.tts.synthesize(text_buffer.strip(), raw_pcm=self._raw_pcm)
                            full_spoken += text_buffer
                            text_buffer = ""
                            for frame in frames:
                                if self._interrupt_event.is_set():
                                    break
                                await self.ws.send_bytes(frame)
                            if first_tts:
                                TelemetryCollector.mark("agent_spk")
                                first_tts = False
                        continue

                    if chunk is None:
                        break  # EOF
                    if self._interrupt_event.is_set():
                        break

                    text_buffer += chunk

                    # Flush on sentence end or enough chars.
                    # First sentence: aggressive (clause-end or 20 chars)
                    # Subsequent: natural (sentence end or 100 chars)
                    is_sentence_end = text_buffer.rstrip().endswith(
                        (".", "!", "?", "\n", "。", "！", "？")
                    )
                    is_clause_end = not is_sentence_end and text_buffer.rstrip().endswith(
                        (",", "，", "、", ":", "：", ";", "；")
                    )
                    should_flush = (
                        (first_tts and (is_clause_end or is_sentence_end or len(text_buffer) >= 20))
                        or (not first_tts and (is_sentence_end or len(text_buffer) >= 100))
                    )
                    if should_flush and text_buffer.strip():
                        if not tts_started:
                            await self.ws.send_json(
                                {
                                    "session_id": self.session_id,
                                    "type": "tts",
                                    "state": "start",
                                }
                            )
                            TelemetryCollector.mark("tts_start")
                            tts_started = True
                        flush_text = text_buffer.strip()
                        text_buffer = ""
                        full_spoken += flush_text
                        frames = await self.tts.synthesize(flush_text, raw_pcm=self._raw_pcm)
                        for frame in frames:
                            if self._interrupt_event.is_set():
                                break
                            await self.ws.send_bytes(frame)
                        if first_tts:
                            TelemetryCollector.mark("agent_spk")
                            first_tts = False

                # Flush remaining
                if text_buffer.strip() and not self._interrupt_event.is_set():
                    if not tts_started:
                        await self.ws.send_json(
                            {
                                "session_id": self.session_id,
                                "type": "tts",
                                "state": "start",
                            }
                        )
                        tts_started = True
                    frames = await self.tts.synthesize(text_buffer.strip(), raw_pcm=self._raw_pcm)
                    full_spoken += text_buffer.strip()
                    for frame in frames:
                        if self._interrupt_event.is_set():
                            break
                        await self.ws.send_bytes(frame)

            except Exception:
                logger.exception("[Voice] TTS consumer failed")
            finally:
                if tts_started:
                    try:
                        await self.ws.send_json(
                            {
                                "session_id": self.session_id,
                                "type": "tts",
                                "state": "stop",
                            }
                        )
                    except Exception:
                        pass

            # Persist spoken text
            if full_spoken and pig and pig.ctx:
                asyncio.create_task(self._persist_turn("assistant", full_spoken))

        # Run in parallel
        prod_task = asyncio.create_task(_producer())
        cons_task = asyncio.create_task(_consumer())
        await prod_task
        await cons_task

    # ── Persistence ──────────────────────────────────────────────────

    async def _persist_turn(self, role: str, content: str) -> None:
        try:
            if self._pig and self._pig.ctx:
                await self._pig.ctx.add_turn(role=role, content=content)
            if role in ("user", "assistant"):
                asyncio.create_task(self._write_roast_conversation(role, content))
        except Exception:
            logger.exception(f"[Voice] Failed to persist {role} turn")

    async def _write_roast_conversation(self, role: str, content: str) -> None:
        try:
            if not self._pig:
                return
            state = await self._pig.get_active_roast()
            if not state:
                return
            from roast.types import Phase

            if state.phase == Phase.CLOSING and role != "assistant":
                return
            if state.phase not in (Phase.ACTIVE, Phase.CLOSING):
                return

            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO roast_conversations
                       (user_id, roast_id, roast_instance_id, role, content)
                       VALUES ($1, $2, $3, $4, $5)""",
                    self._user_id,
                    str(state.roast_id),
                    state.roast_instance_id,
                    role,
                    content,
                )

            redis = get_redis()
            if redis:
                msg = json.dumps(
                    {
                        "type": "roast_message",
                        "roast_id": str(state.roast_id),
                        "role": role,
                        "content": content,
                    }
                )
                await redis.publish(f"ws:user:{self._user_id}", msg)
        except Exception:
            logger.exception("[Voice] Failed to write roast_conversation")

    # ── Roast inject (API server → agent) ────────────────────────────

    async def inject_roast(self, msg: dict) -> None:
        try:
            if self._pig is None:
                logger.warning("[Voice] roast_inject: PigAgent not ready")
                return
            logger.info(
                f"[Voice] roast_inject roast_id={msg.get('roast_id')} "
                f"mode={msg.get('mode_id')}"
            )
            opening_text = ""
            async for text in self._pig.start_roast(
                persona_id=msg.get("persona_id", self._persona_id),
                roast_id=msg["roast_id"],
                mode_id=msg["mode_id"],
                prompt=msg["prompt"],
                headline=msg.get("headline", ""),
                source=msg.get("source", ""),
            ):
                if isinstance(text, str):
                    opening_text += text

            if opening_text.strip() and self.tts:
                sentence_id = self._sentence_id
                self._sentence_id += 1
                await self.ws.send_json(
                    {
                        "session_id": self.session_id,
                        "type": "tts",
                        "state": "start",
                    }
                )
                frames = await self.tts.synthesize(opening_text.strip(), raw_pcm=self._raw_pcm)
                for frame in frames:
                    if self._interrupt_event.is_set():
                        break
                    if self._sentence_id != sentence_id:  # guard stale frames
                        break
                    await self.ws.send_bytes(frame)
                if self._sentence_id == sentence_id:  # only send stop if not interrupted
                    await self.ws.send_json(
                        {
                            "session_id": self.session_id,
                            "type": "tts",
                            "state": "stop",
                        }
                    )
                if self._pig and self._pig.ctx:
                    asyncio.create_task(
                        self._persist_turn("assistant", opening_text.strip())
                    )
        except Exception:
            logger.exception("[Voice] roast_inject failed")

    # ── HTTP session ─────────────────────────────────────────────────

    async def _ensure_http(self) -> Any:
        if self._http is None:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._http = aiohttp.ClientSession(timeout=timeout)
        return self._http

    # ── Cleanup ──────────────────────────────────────────────────────

    async def _cleanup(self) -> None:
        self._interrupt_event.set()
        self._cancel_silence_watchdog()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self.vad and hasattr(self.vad, "release_conn_resources"):
            self.vad.release_conn_resources(self)
        if self._http:
            await self._http.close()
            self._http = None
        logger.info(f"[Voice] Session cleaned session={self.session_id}")
