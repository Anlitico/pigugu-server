"""Connection handler — one instance per device WebSocket.

Follows official xiaozhi-esp32-server architecture:
  - websockets library (not FastAPI/starlette)
  - ThreadPoolExecutor for LLM (non-blocking)
  - Streaming ASR via receive_audio (not batch)
  - Server-side VAD with official double-threshold pattern
  - Pluggable providers (VAD / STT / TTS / LLM)

PigAgent-specific features preserved: metrics, roast inject, persistence.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import tempfile
import time
import uuid
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import websockets
from loguru import logger

from bootstrap.factory import create_pig_agent, get_pg_pool, get_redis
from metrics.session import ColdStartMetrics
from metrics.turn import TelemetryCollector
from providers.base import InterfaceType, STTProvider, TTSProvider, VADProvider

TAG = __name__
TTS_FRAME_INTERVAL = 0.06  # 60 ms per Opus frame at 16 kHz

# ── Opus helpers ──────────────────────────────────────────────────────

def _make_opus_decoder(sample_rate: int = 16000, channels: int = 1) -> Any:
    try:
        import opuslib  # pyright: ignore[reportMissingImports]
        return opuslib.Decoder(sample_rate, channels)
    except ImportError:
        return None


def _decode_opus_packet(data: bytes, decoder: Any) -> bytes | None:
    if not data or decoder is None:
        return data if data and len(data) > 200 else None
    try:
        return decoder.decode(data, 960)  # 60ms at 16kHz
    except Exception:
        return None


# ── Connection handler ────────────────────────────────────────────────

class ConnectionHandler:
    """Per-device WebSocket connection — official xiaozhi architecture."""

    # Set externally before handle_connection
    vad: VADProvider | None = None
    stt: STTProvider | None = None
    tts: TTSProvider | None = None
    executor: ThreadPoolExecutor | None = None

    def __init__(self, client_id: str = ""):
        self.client_id = client_id
        self.session_id = str(uuid.uuid4())[:8]

        # Identity
        self._user_id: str = ""
        self._hw_id: str = ""
        self._persona_id: int = 1
        self._pig: Any = None

        # Audio state (official pattern)
        self.opus_decoder = _make_opus_decoder(16000, 1)
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window: deque[bool] = deque(maxlen=5)
        self.client_listen_mode = "auto"
        self.last_is_voice = False
        self.vad_last_voice_time: float = 0.0
        self._vad_pcm_buffer: bytearray = bytearray()
        self._voice_window: deque[bool] = deque(maxlen=5)

        # ASR audio (official: accumulated PCM for batch fallback)
        self.asr_audio: list[bytes] = []
        self._pre_turn_audio: list[bytes] = []

        # Turn tracking
        self._interrupt_event = asyncio.Event()
        self._sentence_id: int = 0
        self._tts_task: asyncio.Task | None = None
        self._turn_active = False
        self.client_is_speaking = False

        # Silence watchdog (safety net only — VAD should handle normal stop)
        self._watchdog_started_at: float = 0.0
        self._max_listen_seconds: float = float(os.getenv("MAX_LISTEN_SECONDS", "15.0"))
        self._silence_timer: asyncio.Task | None = None

        # Deepgram state (set by STT provider)
        self.dg_connection: Any = None
        self.deepgram_final: asyncio.Event = asyncio.Event()
        self.deepgram_transcript: str = ""

        # Roast / inject
        self._inject_queue: asyncio.Queue[dict] = asyncio.Queue()

        # Cleanup tracking
        self._closed = False

    # ── Main entry: called from websockets server ─────────────────────

    async def handle_connection(self, ws: websockets.ServerConnection) -> None:
        """Official pattern: accept WS, loop dispatch, cleanup."""
        self._ws = ws
        ColdStartMetrics.start(session_id=self.session_id, room_name=self.client_id)
        ColdStartMetrics.mark("entry")
        logger.info(f"[Voice] Connected client={self.client_id} session={self.session_id}")
        self._start_inject_consumer()

        try:
            async for message in ws:
                try:
                    if isinstance(message, str):
                        await self._route_message(json.loads(message))
                    elif isinstance(message, bytes):
                        await self._route_message(message)
                except json.JSONDecodeError:
                    logger.warning(f"[Voice] Bad JSON: {message[:100]}")
                except Exception:
                    logger.exception(f"[Voice] Route error")
        except websockets.ConnectionClosed:
            logger.info(f"[Voice] Connection closed session={self.session_id}")
        except Exception:
            logger.exception(f"[Voice] Error session={self.session_id}")
        finally:
            await self._cleanup()

    # ── Message routing (official: _route_message) ────────────────────

    async def _route_message(self, message) -> None:
        if isinstance(message, dict):
            msg_type = message.get("type", "")
            if msg_type == "hello":
                await self._handle_hello(message)
            elif msg_type == "listen":
                await self._handle_listen(message)
            elif msg_type == "abort":
                await self._handle_abort(message)
            else:
                logger.debug(f"[Voice] Unhandled: {msg_type}")
        elif isinstance(message, bytes):
            if self.vad is None or self.stt is None:
                return
            # Browser sends raw PCM, firmware sends Opus
            if getattr(self, "_raw_pcm", False):
                pcm_frame = message
            else:
                pcm_frame = _decode_opus_packet(message, self.opus_decoder)
            if pcm_frame:
                await self._handle_audio(pcm_frame)

    # ── Hello ─────────────────────────────────────────────────────────

    async def _handle_hello(self, data: dict) -> None:
        self._persona_id = int(data.get("persona_id", 1))
        self._hw_id = str(data.get("hw_id", ""))
        audio_params = data.get("audio_params", {})
        self._raw_pcm = audio_params.get("format", "opus") == "pcm"
        logger.info(
            f"[Voice] Hello client={self.client_id} persona={self._persona_id} "
            f"hw_id={self._hw_id} version={data.get('version')} "
            f"sample_rate={audio_params.get('sample_rate')} "
            f"frame_duration={audio_params.get('frame_duration')}"
        )

        await self._ws.send(json.dumps({
            "type": "hello",
            "transport": "websocket",
            "session_id": self.session_id,
            "audio_params": {
                "format": "pcm" if self._raw_pcm else "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }))

    # ── Listen state machine (official listenMessageHandler) ──────────

    async def _handle_listen(self, data: dict) -> None:
        state = data.get("state", "")
        mode = data.get("mode", "")
        if mode:
            self.client_listen_mode = mode

        if state == "detect":
            await self._on_detect(data)
        elif state == "start":
            await self._on_start()
        elif state == "stop":
            await self._on_stop()

    async def _on_detect(self, data: dict) -> None:
        """Official: reset audio states, start turn."""
        text = data.get("text", "")
        logger.info(f"[Voice] Wake word '{text}' client={self.client_id}")

        self._interrupt_event.clear()
        self._turn_active = True
        self._reset_audio_states()

        TelemetryCollector.start_turn(
            user_id=self._user_id or self.client_id,
            persona_id=self._persona_id,
        )
        TelemetryCollector.mark("vad_start")

        self._start_silence_watchdog()
        logger.info("[Voice] Turn started (detect)")

    async def _on_start(self) -> None:
        """Official: reset audio states when firmware starts a fresh listening session."""
        logger.info("[Voice] Firmware start")
        self._reset_audio_states()
        self._start_silence_watchdog()

    async def _on_stop(self) -> None:
        """Official: finalize ASR, process turn."""
        if not self._turn_active:
            return  # already stopped (guard against double-call)
        self._turn_active = False
        self._cancel_silence_watchdog()
        self.client_voice_stop = True
        TelemetryCollector.mark("vad_end")

        # Always save received audio WAV for debugging
        self._save_input_wav("(stop)")

        if self.stt and self.stt.interface_type == InterfaceType.STREAM:
            # Streaming: get result from Deepgram callback
            await self.stt.handle_voice_stop(self, self.asr_audio)
            transcript = self.deepgram_transcript
        else:
            # Batch fallback
            pcm = b"".join(self.asr_audio)
            transcript = await self.stt.transcribe(pcm) if self.stt else ""

        logger.info(f"[Voice] STT transcript: '{transcript[:200]}'")
        if transcript.strip():
            await self._on_stt_result(transcript.strip())
        else:
            logger.info("[Voice] STT empty, skipping turn")

    # ── Audio handling (official: handleAudioMessage) ─────────────────

    async def _handle_audio(self, pcm_frame: bytes) -> None:
        """Official: VAD → ASR receive_audio. Accumulates audio for batch fallback."""
        if not self._turn_active:
            if self.client_is_speaking:
                # Barge-in: user is speaking during TTS → start a fresh turn
                # to capture this speech for VAD → STT → abort.
                logger.info("[Voice] Barge-in: audio detected during TTS, starting new turn")
                self._turn_active = True
                self._reset_audio_states()
                self._start_silence_watchdog()
                TelemetryCollector.mark("vad_start")
            else:
                # Buffer pre-turn audio (wake word) so it can be sent to Deepgram
                # once the turn becomes active. Previously this was silently dropped.
                self._pre_turn_audio.append(pcm_frame)
                return

        # VAD: official double-threshold pattern
        have_voice = False
        if self.vad is not None:
            have_voice = self.vad.is_vad(self, pcm_frame)

        # Accumulate audio (for batch fallback + debug WAV)
        self.asr_audio.append(pcm_frame)

        # Apply 10x gain for Deepgram (firmware mic level is very low)
        if len(pcm_frame) >= 2:
            arr = np.frombuffer(pcm_frame, dtype=np.int16).astype(np.float32)
            arr *= 10.0
            np.clip(arr, -32768, 32767, out=arr)
            pcm_gained = arr.astype(np.int16).tobytes()
        else:
            pcm_gained = pcm_frame

        # Lazy-open Deepgram on first frame (avoid timeout)
        if self.stt and self.stt.interface_type == InterfaceType.STREAM:
            if not hasattr(self, "_dg_socket"):
                await self.stt.open_audio_channels(self)
                # Flush pre-turn audio (wake word) now that Deepgram is open
                if self._pre_turn_audio:
                    logger.info(f"[Voice] Flushing {len(self._pre_turn_audio)} pre-turn audio frames to Deepgram")
                    for p in self._pre_turn_audio:
                        if len(p) >= 2:
                            arr = np.frombuffer(p, dtype=np.int16).astype(np.float32)
                            arr *= 10.0
                            np.clip(arr, -32768, 32767, out=arr)
                            pg = arr.astype(np.int16).tobytes()
                        else:
                            pg = p
                        await self.stt.receive_audio(self, pg, True)
                        self.asr_audio.append(p)
                    self._pre_turn_audio.clear()
            await self.stt.receive_audio(self, pcm_gained, have_voice)

        # VAD voice → silence transition → auto-stop
        if not have_voice and self.client_voice_stop:
            logger.info(f"[Voice] VAD voice stop, frames={len(self.asr_audio)}")
            self._cancel_silence_watchdog()
            await self._on_stop()

    def _reset_audio_states(self) -> None:
        """Official reset_audio_states."""
        self.client_audio_buffer.clear()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window.clear()
        self.last_is_voice = False
        self.vad_last_voice_time = 0.0
        self._vad_pcm_buffer.clear()
        self._voice_window.clear()
        self.asr_audio.clear()
        # IMPORTANT: do NOT clear _pre_turn_audio here — it contains
        # wake word audio received before the turn became active. It is
        # flushed to Deepgram when the audio channel opens in _handle_audio.

    # ── Silence watchdog ──────────────────────────────────────────────

    def _start_silence_watchdog(self) -> None:
        if self._silence_timer and not self._silence_timer.done():
            self._silence_timer.cancel()
        self._watchdog_started_at = time.time()
        self._silence_timer = asyncio.ensure_future(self._silence_watchdog())

    async def _silence_watchdog(self) -> None:
        while self._turn_active:
            elapsed = time.time() - self._watchdog_started_at
            if elapsed > self._max_listen_seconds:
                logger.warning(f"[Voice] Watchdog ({self._max_listen_seconds}s) auto-stop")
                self._turn_active = False
                if len(self.asr_audio) > 0:
                    await self._on_stop()
                return
            await asyncio.sleep(1.0)

    def _cancel_silence_watchdog(self) -> None:
        if self._silence_timer and not self._silence_timer.done():
            self._silence_timer.cancel()
        self._silence_timer = None

    # ── STT result → LLM → TTS (official: speech_to_text_wrapper → startToChat → chat) ──

    async def _on_stt_result(self, text: str) -> None:
        """Handle STT result: send to client, persist, launch LLM."""
        TelemetryCollector.mark("stt_final")

        # Barge-in: if TTS is still playing when new STT text arrives,
        # abort the current TTS before starting a new conversation.
        if self._tts_task and not self._tts_task.done() and self.client_is_speaking:
            logger.info(f"[Voice] Barge-in: STT text='{text[:60]}' while speaking, aborting TTS")
            self._sentence_id += 1
            self._interrupt_event.set()
            self._tts_task.cancel()
            await self._ws.send(json.dumps({
                "session_id": self.session_id,
                "type": "tts",
                "state": "stop",
            }))
            self.client_is_speaking = False

        await self._ws.send(json.dumps({
            "session_id": self.session_id,
            "type": "stt",
            "text": text,
        }))

        # Create PigAgent (lazy)
        self._user_id = self._user_id or self.client_id
        if self._pig is None:
            self._pig = await create_pig_agent(self._user_id, hw_id=self._hw_id)
            ColdStartMetrics.set_meta("user_id", self._user_id)
            ColdStartMetrics.set_meta("persona_id", self._persona_id)
            ColdStartMetrics.set_meta("llm_model", self._pig.model)
            TelemetryCollector.set_meta("llm_model", self._pig.model)
            ColdStartMetrics.mark("agent_created")
            ColdStartMetrics.mark("ready")
            ColdStartMetrics.flush()

        # Persist user message (must be after _pig creation)
        if self._pig and self._pig.ctx:
            asyncio.ensure_future(self._persist_turn("user", text))

        TelemetryCollector.mark("agent_req")
        TelemetryCollector.mark("llm_start")
        sentence_id = self._sentence_id
        self._sentence_id += 1

        # LLM → TTS
        self._tts_task = asyncio.ensure_future(
            self._tts_producer_consumer(text, sentence_id)
        )

    def _save_input_wav(self, stt_text: str) -> str:
        """Save user speech PCM as WAV. Returns the file path."""
        try:
            pcm = b"".join(self.asr_audio)
            wav_path = f"/tmp/pigugu_in_{self.session_id}.wav"
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm)
            logger.warning(
                f"[Voice] WAV input saved: {wav_path} ({len(pcm)} bytes, "
                f"{len(pcm)/32000:.1f}s) STT='{stt_text[:80]}'"
            )
            return wav_path
        except Exception:
            logger.exception("[Voice] WAV input save failed")
            return ""

    def _save_tts_wav(self, tts_pcm: bytes) -> str:
        """Save TTS output PCM as WAV. Returns the file path."""
        try:
            wav_path = f"/tmp/pigugu_out_{self.session_id}.wav"
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(tts_pcm)
            logger.warning(
                f"[Voice] WAV output saved: {wav_path} ({len(tts_pcm)} bytes, "
                f"{len(tts_pcm)/32000:.1f}s)"
            )
            return wav_path
        except Exception:
            logger.exception("[Voice] TTS WAV save failed")
            return ""

    # ── TTS producer-consumer ─────────────────────────────────────────

    async def _tts_producer_consumer(self, stt_text: str, sentence_id: int) -> None:
        """Official pattern: LLM → text → TTS → Opus → WebSocket."""
        _tts_start = time.time()
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        pig = self._pig
        if pig is None:
            return

        async def _producer() -> str:
            full = ""
            try:
                logger.info(f"[Voice] LLM producer started")
                async for chunk in pig.generate_reply(
                    stt_text,
                    persona_id=self._persona_id,
                    interrupt_event=self._interrupt_event,
                    session_id=self.session_id,
                ):
                    if self._interrupt_event.is_set():
                        logger.info("[Voice] LLM producer interrupted")
                        break
                    logger.info(f"[Voice] LLM chunk type={type(chunk).__name__} len={len(chunk) if hasattr(chunk,'__len__') else '?'}")
                    if isinstance(chunk, str):
                        await text_queue.put(chunk)
                        full += chunk
                logger.info(f"[Voice] LLM producer done: {len(full)} chars")
            except Exception:
                logger.exception("[Voice] LLM producer failed")
            finally:
                await text_queue.put(None)
            return full

        async def _consumer() -> None:
            _cons_start = time.time()
            logger.info(f"[Voice] TTS consumer started, tts={self.tts}")
            if self.tts is None:
                logger.warning("[Voice] TTS provider is None!")
                while True:
                    chunk = await text_queue.get()
                    if chunk is None:
                        break
                return

            text_buffer = ""
            tts_started = False
            first_tts = True
            _first_tts_sent = 0.0
            tts_pcm = bytearray()  # collect raw PCM for debug WAV

            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(text_queue.get(), timeout=0.15)
                    except asyncio.TimeoutError:
                        if text_buffer and self.tts:
                            if not tts_started:
                                await self._ws.send(json.dumps({
                                    "session_id": self.session_id,
                                    "type": "tts",
                                    "state": "start",
                                }))
                                tts_started = True
                                self.client_is_speaking = True
                                TelemetryCollector.mark("tts_start")
                            frames = await self.tts.synthesize(
                                text_buffer.strip(), raw_pcm=self._raw_pcm,
                                collect_pcm=tts_pcm,
                            )
                            text_buffer = ""
                            for frame in frames:
                                if self._interrupt_event.is_set():
                                    break
                                await self._ws.send(frame)
                                await asyncio.sleep(TTS_FRAME_INTERVAL)
                            if first_tts:
                                elapsed = time.time() - _cons_start
                                logger.info(f"[Voice] ⏱ First TTS sent: +{elapsed:.2f}s (consumer→tts)")
                                first_tts = False
                                TelemetryCollector.mark("agent_spk")
                        continue

                    if chunk is None:
                        break
                    if self._interrupt_event.is_set():
                        break

                    text_buffer += chunk
                    is_end = text_buffer.rstrip().endswith((".", "!", "?", "\n", "。", "！", "？"))
                    is_clause = text_buffer.rstrip().endswith((",", "，", "、", ":", "：", ";", "；"))
                    should_flush = (
                        (first_tts and len(text_buffer) >= 10)
                        or (not first_tts and (is_end or len(text_buffer) >= 80))
                    )
                    if should_flush and text_buffer.strip() and self.tts:
                        if not tts_started:
                            await self._ws.send(json.dumps({
                                "session_id": self.session_id,
                                "type": "tts",
                                "state": "start",
                            }))
                            tts_started = True
                            self.client_is_speaking = True
                            TelemetryCollector.mark("tts_start")
                        flush_text = text_buffer.strip()
                        text_buffer = ""
                        frames = await self.tts.synthesize(
                            flush_text, raw_pcm=self._raw_pcm,
                            collect_pcm=tts_pcm,
                        )
                        for frame in frames:
                            if self._interrupt_event.is_set():
                                break
                            await self._ws.send(frame)
                        if first_tts:
                            elapsed = time.time() - _cons_start
                            logger.info(f"[Voice] ⏱ First TTS sent: +{elapsed:.2f}s")
                            first_tts = False
                            TelemetryCollector.mark("agent_spk")

                # Flush remaining
                if text_buffer.strip() and not self._interrupt_event.is_set() and self.tts:
                    if not tts_started:
                        await self._ws.send(json.dumps({
                            "session_id": self.session_id,
                            "type": "tts",
                            "state": "start",
                        }))
                        tts_started = True
                        self.client_is_speaking = True
                        TelemetryCollector.mark("tts_start")
                    frames = await self.tts.synthesize(
                        text_buffer.strip(), raw_pcm=self._raw_pcm,
                        collect_pcm=tts_pcm,
                    )
                    for frame in frames:
                        if self._interrupt_event.is_set():
                            break
                        await self._ws.send(frame)
                    if first_tts:
                        first_tts = False
                        TelemetryCollector.mark("agent_spk")

                # Save TTS output WAV for debugging
                if len(tts_pcm) > 0:
                    self._save_tts_wav(bytes(tts_pcm))

                if tts_started:
                    await self._ws.send(json.dumps({
                        "session_id": self.session_id,
                        "type": "tts",
                        "state": "stop",
                    }))
                    self.client_is_speaking = False
                    TelemetryCollector.mark("tts_end")
                    TelemetryCollector.finish_turn()
            except asyncio.CancelledError:
                self.client_is_speaking = False
                logger.info("[Voice] TTS consumer cancelled")
            except Exception:
                logger.exception("[Voice] TTS consumer failed")

        # Run concurrently
        full_text = ""
        consumer_task = asyncio.ensure_future(_consumer())
        try:
            full_text = await _producer()
            await consumer_task
        except asyncio.CancelledError:
            consumer_task.cancel()

        # Persist assistant message
        if full_text.strip() and self._pig and self._pig.ctx:
            asyncio.ensure_future(self._persist_turn("assistant", full_text.strip()))

    # ── Abort ─────────────────────────────────────────────────────────

    async def _handle_abort(self, data: dict) -> None:
        reason = data.get("reason", "unknown")
        logger.info(f"[Voice] Abort reason={reason}")
        self._sentence_id += 1
        self._interrupt_event.set()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        self.client_is_speaking = False
        await self._ws.send(json.dumps({
            "session_id": self.session_id,
            "type": "tts",
            "state": "stop",
        }))

    # ── Persistence ───────────────────────────────────────────────────

    async def _persist_turn(self, role: str, content: str) -> None:
        try:
            if self._pig and self._pig.ctx:
                await self._pig.ctx.add_turn(role, content)
        except Exception:
            logger.exception(f"[Voice] Persist failed role={role}")

    # ── Roast inject (pigugu-specific) ────────────────────────────────

    async def inject_roast(self, msg: dict) -> None:
        await self._inject_queue.put(msg)

    def _start_inject_consumer(self) -> None:
        asyncio.ensure_future(self._inject_consumer())

    async def _inject_consumer(self) -> None:
        while not self._closed:
            try:
                msg = await asyncio.wait_for(self._inject_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            logger.info(f"[Voice] Inject: {msg.get('type', '?')}")
            sentence_id = self._sentence_id
            self._sentence_id += 1
            if self._tts_task and not self._tts_task.done():
                self._interrupt_event.set()
                self._tts_task.cancel()
            self._interrupt_event.clear()
            await self._ws.send(json.dumps({
                "session_id": self.session_id,
                "type": "tts",
                "state": "start",
            }))
            self._tts_task = asyncio.ensure_future(
                self._inject_tts(msg, sentence_id)
            )
            await self._tts_task

    async def _inject_tts(self, msg: dict, sentence_id: int) -> None:
        try:
            text = msg.get("text", msg.get("content", ""))
            if self.tts and text:
                tts_pcm = bytearray()
                frames = await self.tts.synthesize(text, collect_pcm=tts_pcm)
                for frame in frames:
                    if self._interrupt_event.is_set() or sentence_id != self._sentence_id:
                        break
                    await self._ws.send(frame)
                if len(tts_pcm) > 0:
                    self._save_tts_wav(bytes(tts_pcm))
            await self._ws.send(json.dumps({
                "session_id": self.session_id,
                "type": "tts",
                "state": "stop",
            }))
        except Exception:
            logger.exception("[Voice] Inject TTS failed")

    # ── Cleanup ───────────────────────────────────────────────────────

    async def _cleanup(self) -> None:
        self._closed = True
        self._cancel_silence_watchdog()
        self._interrupt_event.set()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        if self.stt:
            await self.stt.close_audio_channels()
        try:
            if self._pig and hasattr(self._pig, 'ctx'):
                await self._pig.ctx.flush()
        except Exception:
            pass
        logger.info(f"[Voice] Session cleaned session={self.session_id}")
