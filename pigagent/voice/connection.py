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

        # Idle timeout (reference: no_voice_close_connect)
        self.last_voice_activity: float = 0.0
        self._turn_type: str = "follow_up"  # overwritten by _on_detect for wake word
        self._vad_start_marked: bool = False
        self._vad_end_marked: bool = False
        self._barge_in_eligible: bool = False  # new voice detected during TTS
        self._eou_bounce_delay: float = float(os.getenv("EOU_BOUNCE_MS", "500")) / 1000.0
        self._pending_speech_final: str = ""
        self._eou_bounce_task: asyncio.Task | None = None

        # ASR audio (official: accumulated PCM for batch fallback)
        self.asr_audio: list[bytes] = []

        # Turn tracking
        self._interrupt_event = asyncio.Event()
        self._sentence_id: int = 0
        self._tts_task: asyncio.Task | None = None
        self.client_is_speaking = False

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
        self._loop = asyncio.get_running_loop()
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
                logger.warning(f"[Voice] DIAG binary dropped: vad={self.vad is not None} stt={self.stt is not None}")
                return
            # Diagnostic: count binary messages
            if not hasattr(self, "_diag_bin_count"):
                self._diag_bin_count = 0
            self._diag_bin_count += 1
            # Browser sends raw PCM, firmware sends Opus
            if getattr(self, "_raw_pcm", False):
                pcm_frame = message
            else:
                pcm_frame = _decode_opus_packet(message, self.opus_decoder)
                if self._diag_bin_count <= 3 or self._diag_bin_count % 50 == 0:
                    logger.info(
                        f"[Voice] DIAG bin #{self._diag_bin_count} opus_in={len(message)} "
                        f"pcm_out={'OK' if pcm_frame else 'NONE'} decoder={'OK' if self.opus_decoder else 'MISSING'}"
                    )
            if pcm_frame:
                if self._diag_bin_count <= 3:
                    # Show PCM energy
                    arr = np.frombuffer(pcm_frame, dtype=np.int16).astype(np.float32)
                    rms = float(np.sqrt(np.mean(arr ** 2)))
                    logger.info(f"[Voice] DIAG pcm #{self._diag_bin_count} len={len(pcm_frame)} rms={rms:.1f}")
                await self._handle_audio(pcm_frame)
            elif self._diag_bin_count <= 3:
                logger.warning(f"[Voice] DIAG pcm_frame is None, skipping _handle_audio")

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
        elif state == "vad_silence":
            await self._on_vad_silence(data)
        elif state == "start":
            await self._on_start()
        elif state == "stop":
            # Client stopped listening — Deepgram handles endpointing automatically
            self._reset_audio_states()

    async def _on_detect(self, data: dict) -> None:
        """Wake word from client: reset audio states, suppress VAD briefly."""
        text = data.get("text", "")
        logger.info(f"[Voice] Wake word '{text}' client={self.client_id}")

        self._turn_type = "wake_word"
        self._interrupt_event.clear()
        self._reset_audio_states()

        # Suppress VAD for 2s to prevent wake word audio from triggering
        # a false voice detection (reference: just_woken_up)
        self.just_woken_up = True
        if hasattr(self, "vad_resume_task") and self.vad_resume_task and not self.vad_resume_task.done():
            self.vad_resume_task.cancel()
        self.vad_resume_task = asyncio.ensure_future(self._resume_vad())

        TelemetryCollector.start_turn(
            user_id=self._user_id or self.client_id,
            persona_id=self._persona_id,
        )
        TelemetryCollector.set_meta("turn_type", "wake_word")

    async def _on_vad_silence(self, data: dict) -> None:
        """Firmware VAD silence — stores user_stop_ms for E2E on every turn."""
        user_stop_ms = data.get("user_stop_ms", 0)
        if user_stop_ms > 0:
            self._e2e_user_stop = user_stop_ms / 1000.0
            self._e2e_detect = time.time()

    async def _resume_vad(self) -> None:
        await asyncio.sleep(2)
        self.just_woken_up = False
        logger.debug("[Voice] VAD resumed after wake word suppression")

    async def _on_start(self) -> None:
        """Client listening session start: reset audio states."""
        logger.info("[Voice] Firmware start")
        self._reset_audio_states()

    # ── Audio handling (official: handleAudioMessage) ─────────────────

    async def _handle_audio(self, pcm_frame: bytes) -> None:
        """Reference pattern: VAD always runs, Deepgram always receives.
        No turn gate — VAD provider manages voice state internally."""
        # 1. VAD: always active on every frame
        have_voice = False
        if self.vad is not None:
            have_voice = self.vad.is_vad(self, pcm_frame)

        # Suppress VAD after wake word to avoid false trigger (reference: just_woken_up)
        if getattr(self, "just_woken_up", False):
            have_voice = False

        # Track if new voice was detected during TTS → eligible for barge-in.
        # Use both confirmed voice (sliding window) and per-frame voice
        # (faster, catches brief barge-in speech the window might miss).
        is_voice_frame = getattr(self, "last_is_voice", False)
        if (have_voice or is_voice_frame) and self.client_is_speaking and not self._barge_in_eligible:
            self._barge_in_eligible = True
            logger.info("[Voice] Barge-in eligible (have_voice=%s is_voice_frame=%s)", have_voice, is_voice_frame)

        # Cancel EOU bounce if user resumes speaking (LiveKit pattern)
        if have_voice and self._pending_speech_final:
            logger.info("[Voice] EOU bounce cancelled — user resumed speaking")
            self._pending_speech_final = ""
            if self._eou_bounce_task and not self._eou_bounce_task.done():
                self._eou_bounce_task.cancel()

        # Latency marks: vad_start / vad_end based on VAD state transitions.
        # Lazily start turn for follow-up utterances (wake word started in _on_detect).
        if self.client_have_voice and not self._vad_start_marked:
            if getattr(self, "_turn_type", "follow_up") != "wake_word":
                TelemetryCollector.start_turn(
                    user_id=self._user_id or self.client_id,
                    persona_id=self._persona_id,
                )
                TelemetryCollector.set_meta("turn_type", "follow_up")
            TelemetryCollector.mark("vad_start")
            self._vad_start_marked = True
        if self.client_voice_stop and not self._vad_end_marked:
            TelemetryCollector.mark("vad_end")
            self._vad_end_marked = True

        # Track voice activity (reference: no_voice_close_connect)
        now_ms = time.time() * 1000
        if have_voice:
            self.last_voice_activity = now_ms

        # 2. Accumulate + gain + feed Deepgram (always)
        self.asr_audio.append(pcm_frame)
        if len(pcm_frame) >= 2:
            arr = np.frombuffer(pcm_frame, dtype=np.int16).astype(np.float32)
            arr *= 10.0
            np.clip(arr, -32768, 32767, out=arr)
            pcm_gained = arr.astype(np.int16).tobytes()
        else:
            pcm_gained = pcm_frame

        if self.stt and self.stt.interface_type == InterfaceType.STREAM:
            if not hasattr(self, "_dg_socket"):
                await self.stt.open_audio_channels(self)
            await self.stt.receive_audio(self, pcm_gained, have_voice)

        # 3. Idle timeout: close WS after 120s of no voice (reference: no_voice_close_connect)
        if self.last_voice_activity > 0 and not self.client_is_speaking:
            idle_sec = (now_ms - self.last_voice_activity) / 1000
            _idle_timeout = float(os.getenv("VOICE_IDLE_TIMEOUT_SEC", "120"))
            if idle_sec > _idle_timeout:
                logger.info(f"[Voice] Idle {idle_sec:.0f}s, closing connection")
                await self._ws.close()

    async def _on_stt_final(self, text: str) -> None:
        """Deepgram speech_final — start EOU bounce timer before committing."""
        logger.info(f"[Voice] STT final (bounce {self._eou_bounce_delay*1000:.0f}ms): '{text[:200]}'")
        self._pending_speech_final = text.strip()
        # Cancel any previous bounce
        if self._eou_bounce_task and not self._eou_bounce_task.done():
            self._eou_bounce_task.cancel()
        self._eou_bounce_task = asyncio.ensure_future(self._eou_bounce())

    async def _eou_bounce(self) -> None:
        """LiveKit pattern: wait bounce delay, then commit if not cancelled."""
        await asyncio.sleep(self._eou_bounce_delay)
        text = self._pending_speech_final
        self._pending_speech_final = ""
        if not text:
            return  # cancelled by VAD detecting new voice
        logger.info(f"[Voice] EOU bounce complete, committing: '{text[:120]}'")
        # Clear Deepgram buffer — utterance committed
        if hasattr(self, "_dg_final_buffer"):
            self._dg_final_buffer.clear()
        await self._on_stt_result(text)
        self._reset_audio_states()
        self._turn_type = "follow_up"  # next VAD detection starts a follow-up turn

    def _log_e2e_if_present(self) -> None:
        """Log wall-clock E2E: user_stop(固件) → agent_spk(服务端).
        Segments:
          fw_gap:      user_stop → detect_received   (WS + audio buffer)
          server_stt:  detect_received → stt_final    (VAD + Deepgram)
          llm_tts:     stt_final → agent_spk          (LLM + Cartesia)
        """
        user_stop = getattr(self, "_e2e_user_stop", 0.0)
        if user_stop <= 0:
            return
        now = time.time()
        fw_gap = getattr(self, "_e2e_fw_gap", 0.0)
        server_stt = getattr(self, "_e2e_server_stt", 0.0)
        llm_tts = round(now - getattr(self, "_e2e_stt_start", now), 3)
        e2e = round(now - user_stop, 3)
        logger.info(
            f"[Voice] ⏱ TRUE E2E={e2e:.3f}s "
            f"(fw_gap={fw_gap:.3f}s server_stt={server_stt:.3f}s llm_tts={llm_tts:.3f}s)"
        )
        TelemetryCollector.set_meta("e2e_true_s", e2e)
        TelemetryCollector.set_meta("fw_gap_s", fw_gap)
        TelemetryCollector.set_meta("server_stt_s", server_stt)
        TelemetryCollector.set_meta("llm_tts_s", llm_tts)
        self._e2e_user_stop = 0  # one-shot

    def _reset_audio_states(self) -> None:
        """Official reset_audio_states."""
        # Save accumulated audio before clearing — captures barge-in speech
        if self.asr_audio:
            try:
                self._save_input_wav(f"(turn_{self._sentence_id})")
            except Exception:
                pass
        self.client_audio_buffer.clear()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window.clear()
        self.last_is_voice = False
        self.vad_last_voice_time = 0.0
        self._vad_pcm_buffer.clear()
        self._voice_window.clear()
        self.asr_audio.clear()
        self.just_woken_up = False
        self._vad_start_marked = False
        self._vad_end_marked = False
        self._barge_in_eligible = False
        self._pending_speech_final = ""
        if self._eou_bounce_task and not self._eou_bounce_task.done():
            self._eou_bounce_task.cancel()

    # ── Silence watchdog ──────────────────────────────────────────────

    # ── STT result → LLM → TTS (official: speech_to_text_wrapper → startToChat → chat) ──

    async def _on_stt_result(self, text: str) -> None:
        """Handle STT result: send to client, persist, launch LLM."""
        TelemetryCollector.mark("stt_final")

        # E2E wall-clock tracking: firmware user_stop → server segments
        user_stop = getattr(self, "_e2e_user_stop", 0.0)
        if user_stop > 0:
            self._e2e_fw_gap = round(self._e2e_detect - user_stop, 3)    # firmware→detect
            self._e2e_server_stt = round(time.time() - self._e2e_detect, 3) # detect→STT final
            self._e2e_stt_start = time.time()  # start of LLM+Cartesia segment
            TelemetryCollector.set_meta("fw_gap_s", self._e2e_fw_gap)

        # Barge-in: only if the user *actively interrupted* (new voice detected
        # during TTS). Multi-sentence finals from Deepgram's endpointing on
        # the same audio stream should NOT trigger barge-in.
        if self._tts_task and not self._tts_task.done() and self.client_is_speaking:
            if getattr(self, "_barge_in_eligible", False):
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
                self._barge_in_eligible = False
            else:
                # Same utterance, continue current TTS — don't abort
                logger.info(f"[Voice] Skipping STT (TTS still playing, no new voice): '{text[:60]}'")
                return

        await self._ws.send(json.dumps({
            "session_id": self.session_id,
            "type": "stt",
            "text": text,
        }))

        # Create PigAgent (lazy)
        self._user_id = self._user_id or self.client_id
        if self._pig is None:
            self._pig = await create_pig_agent(self._user_id, hw_id=self._hw_id)
            TelemetryCollector.set_meta("llm_model", self._pig.model)

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
            sid = getattr(self, "_sentence_id", 0)
            wav_path = f"/tmp/pigugu_in_{self.session_id}_{sid}.wav"
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
                                self._log_e2e_if_present()
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
                            self._log_e2e_if_present()

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
                        self._log_e2e_if_present()

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
        self._interrupt_event.set()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        # Save accumulated audio even if STT never fired — diagnostic
        if self.asr_audio:
            try:
                self._save_input_wav("(no_stt_result)")
            except Exception:
                pass
        if self.stt:
            await self.stt.close_audio_channels()
        try:
            if self._pig and hasattr(self._pig, 'ctx'):
                await self._pig.ctx.flush()
        except Exception:
            pass
        logger.info(f"[Voice] Session cleaned session={self.session_id}")
