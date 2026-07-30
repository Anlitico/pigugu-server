# pigagent/ws/handler.py
"""Xiaozhi WebSocket protocol handler.

One handler instance per device connection. Maps xiaozhi protocol messages
(hello/listen/abort/Opus binary) to PigAgent pipeline calls.

Protocol flow:
  Client -> Server:
    {"type":"hello","version":1,"transport":"websocket","audio_params":{...}}
    {"type":"listen","state":"detect","text":"Hi fairy"}
    {"type":"listen","state":"start","mode":"auto"}
    [binary Opus audio frames — one per WS message]
    {"type":"listen","state":"stop"}
    {"type":"abort"}

  Server -> Client:
    {"type":"hello","transport":"websocket","session_id":"xxx","audio_params":{...}}
    {"type":"stt","text":"..."}
    {"type":"tts","state":"start"}
    [binary Opus audio frames]
    {"type":"tts","state":"stop"}
    {"type":"llm","emotion":"..."}
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
import time
import numpy as np
from typing import Any

from loguru import logger
from starlette.websockets import WebSocket, WebSocketDisconnect

from bootstrap.factory import create_pig_agent, get_pg_pool, get_redis
from metrics.session import ColdStartMetrics
from metrics.turn import TelemetryCollector

# ── Silero VAD (lazy-loaded per worker, ONNX-based, no PyTorch) ─────
_vad_model = None


def _get_vad_model():
    global _vad_model
    if _vad_model is None:
        from silero_vad_lite import SileroVAD
        _vad_model = SileroVAD(16000)
    return _vad_model


class XiaozhiHandler:
    """Per-connection xiaozhi protocol handler."""

    def __init__(self, ws: WebSocket, client_id: str = ""):
        self.ws = ws
        self.client_id = client_id
        self.session_id = str(uuid.uuid4())[:8]
        self._user_id: str = ""
        self._hw_id: str = ""
        self._pig = None
        self._audio_frames: list[bytes] = []
        self._listening = False
        self._interrupt_event = asyncio.Event()
        self._tts_task: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        self._persona_id: int = 1
        self._sentence_id: int = 0
        self._silence_timer: asyncio.Task | None = None
        self._vad_pcm: list[np.ndarray] = []  # accumulated PCM for VAD
        self._vad_speech_detected = False
        self._pre_buffer: list[bytes] = []  # frames buffered before listening starts
        self._http: Any = None  # shared aiohttp session — created lazily

    # ── Main dispatch ───────────────────────────────────────────────

    async def run(self) -> None:
        """Accept WS and dispatch messages until disconnect."""
        await self.ws.accept()
        ColdStartMetrics.start(session_id=self.session_id, room_name=self.client_id)
        ColdStartMetrics.mark("entry")
        logger.info(f"[Xiaozhi] Connected: client_id={self.client_id} session={self.session_id}")

        try:
            while True:
                msg = await self.ws.receive()
                if "text" in msg:
                    await self._handle_json(json.loads(msg["text"]))
                elif "bytes" in msg:
                    await self._handle_binary(msg["bytes"])
        except WebSocketDisconnect:
            logger.info(f"[Xiaozhi] Disconnected: {self.session_id}")
        except Exception as e:
            logger.error(f"[Xiaozhi] Error: {e}")
        finally:
            await self._cleanup()

    # ── JSON handlers ──────────────────────────────────────────────

    async def _handle_json(self, data: dict) -> None:
        msg_type = data.get("type", "")
        if msg_type == "hello":
            await self._handle_hello(data)
        elif msg_type == "listen":
            await self._handle_listen(data)
        elif msg_type == "abort":
            await self._handle_abort(data)
        else:
            logger.debug(f"[Xiaozhi] Unhandled message type: {msg_type}")

    async def _handle_hello(self, data: dict) -> None:
        """Respond to client hello with server hello."""
        transport = data.get("transport", "")
        if transport != "websocket":
            await self.ws.send_json({"type": "hello", "transport": transport, "session_id": self.session_id})
            return

        # Extract device metadata from hello message
        self._persona_id = int(data.get("persona_id", 1))
        self._hw_id = str(data.get("hw_id", ""))

        audio_params = data.get("audio_params", {})
        logger.info(
            f"[Xiaozhi] Hello from {self.client_id}: "
            f"persona={self._persona_id} hw_id={self._hw_id} "
            f"version={data.get('version')} "
            f"sample_rate={audio_params.get('sample_rate')} "
            f"frame_duration={audio_params.get('frame_duration')}"
        )

        response = {
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
        await self.ws.send_json(response)

    async def _handle_listen(self, data: dict) -> None:
        """Handle listen state transitions."""
        state = data.get("state", "")

        if state == "detect":
            text = data.get("text", "")
            logger.info(f"[Xiaozhi] Wake word detected: '{text}' from {self.client_id}")

            # Start collecting audio from detect — wake word frames arrived
            # before detect and were buffered in _pre_buffer; we flush them
            # now so the wake word audio is included in the STT input.
            if self._turn_task and not self._turn_task.done():
                self._sentence_id += 1
                self._interrupt_event.set()
                self._turn_task.cancel()
                logger.info(f"[Xiaozhi] Aborted in-flight turn for new listen/detect")

            self._interrupt_event.clear()
            self._listening = True
            self._audio_frames = list(self._pre_buffer)
            self._pre_buffer.clear()
            self._vad_pcm.clear()
            self._vad_speech_detected = False
            # Run VAD on pre-buffered frames so silence watchdog has context
            for data in self._audio_frames:
                pcm = _opus_decode_one(data, sample_rate=16000, channels=1)
                if pcm and len(pcm) >= 2:
                    if len(pcm) % 2 != 0:
                        pcm = pcm[:-1]
                    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                    self._vad_pcm.append(arr)
            if len(self._vad_pcm) >= 5:
                merged = np.concatenate(self._vad_pcm)
                self._vad_pcm.clear()
                if self._run_vad(merged):
                    self._vad_speech_detected = True
            self._start_silence_watchdog()
            logger.info(f"[Xiaozhi] Listening start (from detect): {len(self._audio_frames)} pre-buffered frames")

        elif state == "start":
            # Don't clear audio — frames are already being collected since detect.
            # Just restart the watchdog as the firmware is now streaming live audio.
            logger.info(f"[Xiaozhi] Firmware start: {len(self._audio_frames)} frames so far")
            self._start_silence_watchdog()

        elif state == "stop":
            self._listening = False
            self._cancel_silence_watchdog()
            logger.info(f"[Xiaozhi] Listening stop: {len(self._audio_frames)} frames")
            if len(self._audio_frames) > 0:
                self._turn_task = asyncio.create_task(self._process_turn())

    # ── Server-side VAD + silence detection ────────────────────────

    def _run_vad(self, audio: np.ndarray) -> bool:
        """Returns True if speech is present in the PCM chunk."""
        try:
            model = _get_vad_model()
            # silero-vad-lite: process() returns float speech probability
            return model.process(audio) > 0.5
        except Exception:
            return True  # assume speech on VAD error → no false-timeout

    STATIC_SILENCE_TIMEOUT = 1.5  # seconds

    def _start_silence_watchdog(self) -> None:
        """Reset or start the silence timer."""
        if self._silence_timer and not self._silence_timer.done():
            self._silence_timer.cancel()
        self._silence_timer = asyncio.create_task(self._silence_watchdog())

    async def _silence_watchdog(self) -> None:
        await asyncio.sleep(self.STATIC_SILENCE_TIMEOUT)
        if self._listening and len(self._audio_frames) > 0:
            logger.info(f"[Xiaozhi] Auto-stop on silence: {len(self._audio_frames)} frames")
            self._listening = False
            self._turn_task = asyncio.create_task(self._process_turn())

    def _cancel_silence_watchdog(self) -> None:
        if self._silence_timer and not self._silence_timer.done():
            self._silence_timer.cancel()
        self._silence_timer = None

    async def _handle_abort(self, data: dict) -> None:
        """Abort current TTS/LLM output."""
        reason = data.get("reason", "unknown")
        logger.info(f"[Xiaozhi] Abort: reason={reason} session={self.session_id}")
        self._sentence_id += 1  # invalidate stale TTS frames
        self._interrupt_event.set()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()

    # ── Binary audio handler ────────────────────────────────────────

    async def _handle_binary(self, data: bytes) -> None:
        """Store Opus frames, run VAD, reset silence timer."""
        if not self._listening:
            # Buffer frames that arrive before detect (e.g. wake word audio).
            # Keep a sliding window so we don't grow unbounded.
            self._pre_buffer.append(data)
            if len(self._pre_buffer) > 200:
                self._pre_buffer.pop(0)
            return
        self._audio_frames.append(data)
        # Decode and feed VAD
        pcm = _opus_decode_one(data, sample_rate=16000, channels=1)
        if pcm and len(pcm) >= 2:
            # Ensure even-length for int16 (strip odd trailing byte if present)
            if len(pcm) % 2 != 0:
                pcm = pcm[:-1]
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            self._vad_pcm.append(arr)
            # Check VAD every ~300ms (5 × 60ms frames)
            if len(self._vad_pcm) >= 5:
                merged = np.concatenate(self._vad_pcm)
                self._vad_pcm.clear()
                speech = self._run_vad(merged)
                if speech and not self._vad_speech_detected:
                    self._vad_speech_detected = True
                    logger.debug(f"[Xiaozhi] VAD speech start: session={self.session_id}")
        # Reset silence watchdog on every frame
        self._start_silence_watchdog()

    # ── Turn processing pipeline ────────────────────────────────────

    async def _process_turn(self) -> None:
        """Opus → PCM → STT → PigAgent → TTS → Opus → WS.

        LLM and TTS run in parallel via producer-consumer pattern:
        _llm_producer feeds text into a queue; _tts_consumer reads
        from queue and flushes to Cartesia at punctuation boundaries.
        First flush is aggressive (any punctuation) for low latency;
        subsequent flushes use sentence-end only for natural phrasing.
        """
        frames = list(self._audio_frames)
        self._audio_frames.clear()
        logger.warning(f"[Xiaozhi] _process_turn ENTER: {len(frames)} frames")
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
            logger.warning(f"[Xiaozhi] STEP-A: Opus decode {len(frames)} frames")
            pcm_frames = [_opus_decode_one(f, sample_rate=16000, channels=1) for f in frames]
            logger.warning(f"[Xiaozhi] STEP-B: decoded, joining")
            pcm = b"".join([pf for pf in pcm_frames if pf])
            logger.warning(f"[Xiaozhi] STEP-C: PCM={len(pcm)} bytes")
            if len(pcm) < 1600:
                logger.warning(f"[Xiaozhi] STEP-D: exit, audio too short")
                return

            # 2. STT
            logger.warning(f"[Xiaozhi] STEP-E: calling Deepgram...")
            stt_text = await self._transcribe(pcm)
            logger.warning(f"[Xiaozhi] STEP-F: Deepgram='{stt_text[:80]}'")
            if not stt_text.strip():
                logger.debug("[Xiaozhi] STT produced no text")
                return

            TelemetryCollector.mark("stt_final")
            logger.info(f"[Xiaozhi] STT: '{stt_text[:120]}'")

            await self.ws.send_json({
                "session_id": self.session_id,
                "type": "stt",
                "text": stt_text.strip(),
            })

            # Persist user message
            if self._pig and self._pig.ctx:
                asyncio.create_task(self._persist_turn("user", stt_text.strip()))

            # 3. Create PigAgent (once per connection)
            self._user_id = self._user_id or self.client_id
            if self._pig is None:
                self._pig = await create_pig_agent(self._user_id, hw_id=self._hw_id)
                ColdStartMetrics.set_meta("user_id", self._user_id)
                ColdStartMetrics.set_meta("persona_id", self._persona_id)
                ColdStartMetrics.set_meta("llm_model", self._pig.model)
                TelemetryCollector.set_meta("llm_model", self._pig.model)
                ColdStartMetrics.mark("agent_created")
                ColdStartMetrics.flush()

            TelemetryCollector.mark("agent_req")

            # 4. LLM → TTS streaming: two parallel tasks, producer-consumer pattern.
            #    LLM chunks flow into a queue; TTS reads from queue, sends to
            #    Cartesia as text accumulates, and pushes Opus frames to WS.
            #    Both tasks respect _interrupt_event for barge-in.
            text_queue: asyncio.Queue[str | None] = asyncio.Queue()

            pig = self._pig
            assert pig is not None, "PigAgent not initialized"
            async def _llm_producer():
                """Feed LLM text chunks into the queue; sends None as EOF."""
                try:
                    async for chunk in pig.generate_reply(
                        stt_text.strip(),
                        persona_id=self._persona_id,
                        interrupt_event=self._interrupt_event,
                        session_id=self.session_id,
                    ):
                        if self._interrupt_event.is_set():
                            break
                        if isinstance(chunk, str):
                            await text_queue.put(chunk)
                        else:
                            logger.info(f"[Xiaozhi] LLM non-str chunk type={type(chunk).__name__}")
                    await text_queue.put(None)  # EOF
                    logger.info(f"[Xiaozhi] LLM producer done")
                except Exception as e:
                    logger.error(f"[Xiaozhi] LLM producer failed: {e}")
                    await text_queue.put(None)

            async def _tts_consumer():
                """Read text from queue, flush to Cartesia in chunks, send Opus."""
                logger.info(f"[Xiaozhi] TTS consumer started")
                text_buffer = ""
                full_spoken = ""
                first_tts = True
                tts_started = False
                api_key = os.getenv("CARTESIA_API_KEY", "")
                voice_id = os.getenv("CARTESIA_TTS_VOICE", "9783574a-63f4-46bf-b56b-928eb52d3140")
                model_id = os.getenv("CARTESIA_TTS_MODEL", "sonic-3.5")
                url = "https://api.cartesia.ai/tts/bytes"

                try:
                    while True:
                        try:
                            chunk = await asyncio.wait_for(text_queue.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            # No new text — flush what we have if enough
                            if text_buffer:
                                if not tts_started:
                                    await self.ws.send_json({"session_id": self.session_id, "type": "tts", "state": "start"})
                                    TelemetryCollector.mark("tts_start")
                                    tts_started = True
                                full_spoken += text_buffer
                                await self._cartesia_flush(text_buffer.strip(), api_key, voice_id, model_id, url)
                                text_buffer = ""
                                if first_tts:
                                    TelemetryCollector.mark("agent_spk")
                                    first_tts = False
                            continue

                        if chunk is None:
                            logger.info(f"[Xiaozhi] TTS consumer got EOF, full_spoken={len(full_spoken)}")
                            break  # EOF
                        if self._interrupt_event.is_set():
                            break

                        text_buffer += chunk

                        # Flush to TTS on sentence end or enough chars
                        # xiaozhi + Deepgram pattern:
                        # First sentence: aggressive (any punctuation), minimize first-audio.
                        # Subsequent: natural (sentence end only), up to 100 chars max.
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
                                await self.ws.send_json({"session_id": self.session_id, "type": "tts", "state": "start"})
                                TelemetryCollector.mark("tts_start")
                                tts_started = True
                            flush_text = text_buffer.strip()
                            text_buffer = ""
                            full_spoken += flush_text
                            await self._cartesia_flush(flush_text, api_key, voice_id, model_id, url)
                            if first_tts:
                                TelemetryCollector.mark("agent_spk")
                                first_tts = False

                    # Flush remaining
                    if text_buffer.strip() and not self._interrupt_event.is_set():
                        if not tts_started:
                            await self.ws.send_json({"session_id": self.session_id, "type": "tts", "state": "start"})
                            TelemetryCollector.mark("tts_start")
                            tts_started = True
                        flush_text = text_buffer.strip()
                        full_spoken += flush_text
                        await self._cartesia_flush(flush_text, api_key, voice_id, model_id, url)
                        if first_tts:
                            TelemetryCollector.mark("agent_spk")
                except Exception as e:
                    logger.error(f"[Xiaozhi] TTS consumer failed: {e}")
                finally:
                    if tts_started:
                        await self.ws.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})
                    return full_spoken.strip()

            # Run producer and consumer in parallel
            self._tts_task = asyncio.create_task(_tts_consumer())
            llm_task = asyncio.create_task(_llm_producer())
            await llm_task  # wait for LLM to finish
            full_spoken = await self._tts_task  # wait for TTS to finish

            # Persist only what was actually spoken (TTS output before interrupt)
            if full_spoken and self._pig and self._pig.ctx:
                asyncio.create_task(self._persist_turn("assistant", full_spoken))

        except asyncio.CancelledError:
            logger.info(f"[Xiaozhi] Turn cancelled sid={sentence_id}")
        except Exception as e:
            logger.error(f"[Xiaozhi] Turn processing failed: {e}")
        finally:
            TelemetryCollector.finish_turn()

    async def _transcribe(self, pcm: bytes) -> str:
        """Send PCM to Deepgram streaming endpoint, return transcript."""
        try:
            api_key = os.getenv("DEEPGRAM_API_KEY", "")
            if not api_key:
                logger.error("[Xiaozhi] DEEPGRAM_API_KEY not set")
                return ""

            url = "https://api.deepgram.com/v1/listen?model=nova-3&language=en&encoding=linear16&sample_rate=16000"
            http = await self._ensure_http()
            async with http.post(url, data=pcm, headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/x-raw",
            }) as resp:
                if resp.status != 200:
                    err_body = await resp.text()
                    logger.error(f"[Xiaozhi] Deepgram error: {resp.status} body={err_body[:500]}")
                    return ""
                result = await resp.json()
                channel = result.get("results", {}).get("channels", [{}])[0]
                alternatives = channel.get("alternatives", [{}])
                return alternatives[0].get("transcript", "")
        except Exception as e:
            logger.error(f"[Xiaozhi] Deepgram API call failed: {e}")
            return ""

    async def _cartesia_flush(self, text: str, api_key: str, voice_id: str,
                               model_id: str, url: str) -> None:
        """Single Cartesia TTS call → Opus encode → WS send."""
        http = await self._ensure_http()
        payload = _make_tts_payload(text, voice_id, model_id)
        async with http.post(
            url, json=payload,
            headers={"X-API-Key": api_key, "Cartesia-Version": "2024-06-10"},
        ) as resp:
            if resp.status != 200:
                logger.error(f"[Xiaozhi] Cartesia error: {resp.status}")
                return
            pcm = bytearray()
            async for data in resp.content.iter_chunked(4096):
                if self._interrupt_event.is_set():
                    break
                pcm.extend(data)
        if len(pcm) > 0 and not self._interrupt_event.is_set():
            for frame in opus_encode_chunks(
                bytes(pcm), sample_rate=16000, channels=1, frame_duration_ms=60,
            ):
                if self._interrupt_event.is_set():
                    break
                await self.ws.send_bytes(frame)

    async def _cartesia_speak(self, text: str, sentence_id: int) -> None:
        """Speak text via Cartesia TTS → Opus → WS. Respects interrupt + sentence_id."""
        await self.ws.send_json({"session_id": self.session_id, "type": "tts", "state": "start"})
        TelemetryCollector.mark("tts_start")
        try:
            api_key = os.getenv("CARTESIA_API_KEY", "")
            voice_id = os.getenv("CARTESIA_TTS_VOICE", "9783574a-63f4-46bf-b56b-928eb52d3140")
            model_id = os.getenv("CARTESIA_TTS_MODEL", "sonic-3.5")

            http = await self._ensure_http()
            payload = _make_tts_payload(text, voice_id, model_id)
            async with http.post(
                "https://api.cartesia.ai/tts/bytes",
                json=payload,
                headers={"X-API-Key": api_key, "Cartesia-Version": "2024-06-10"},
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[Xiaozhi] Cartesia error: {resp.status}")
                    return
                pcm_chunks = bytearray()
                async for data in resp.content.iter_chunked(4096):
                    if self._interrupt_event.is_set():
                        break
                    pcm_chunks.extend(data)

            if len(pcm_chunks) > 0 and not self._interrupt_event.is_set():
                for frame in opus_encode_chunks(
                    bytes(pcm_chunks), sample_rate=16000, channels=1, frame_duration_ms=60,
                ):
                    if self._interrupt_event.is_set():
                        break
                    if self._sentence_id != sentence_id:
                        break
                    await self.ws.send_bytes(frame)
        except asyncio.CancelledError:
            logger.info(f"[Xiaozhi] TTS cancelled sid={sentence_id}")
        except Exception as e:
            logger.error(f"[Xiaozhi] TTS failed: {e}")
        finally:
            if self._sentence_id == sentence_id:
                await self.ws.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})

    # ── Persistence ─────────────────────────────────────────────────

    async def _persist_turn(self, role: str, content: str) -> None:
        """Persist a turn to context + roast_conversations (for App display)."""
        try:
            if self._pig and self._pig.ctx:
                await self._pig.ctx.add_turn(role=role, content=content)
            if role in ("user", "assistant"):
                asyncio.create_task(self._write_roast_conversation(role, content))
        except Exception as e:
            logger.error(f"[Xiaozhi] Failed to persist {role} turn: {e}")

    async def _write_roast_conversation(self, role: str, content: str) -> None:
        """Write user/assistant message to roast_conversations table + push to App WS."""
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
                import json as _json
                msg = _json.dumps({
                    "type": "roast_message",
                    "roast_id": str(state.roast_id),
                    "role": role,
                    "content": content,
                })
                await redis.publish(f"ws:user:{self._user_id}", msg)
        except Exception as e:
            logger.error(f"[Xiaozhi] Failed to write roast_conversation: {e}")

    # ── Roast inject (API server → agent) ──

    async def inject_roast(self, msg: dict) -> None:
        """Handle roast_inject command from the API server."""
        try:
            if self._pig is None:
                logger.warning("[Xiaozhi] roast_inject: PigAgent not ready")
                return
            logger.info(
                f"[Xiaozhi] roast_inject: roast_id={msg.get('roast_id')} "
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

            if opening_text.strip():
                sentence_id = self._sentence_id
                self._sentence_id += 1
                self._tts_task = asyncio.create_task(
                    self._cartesia_speak(opening_text.strip(), sentence_id)
                )
                await self._tts_task
                if self._pig and self._pig.ctx:
                    asyncio.create_task(
                        self._persist_turn("assistant", opening_text.strip())
                    )
        except Exception as exc:
            logger.error(f"[Xiaozhi] roast_inject failed: {exc}")

    # ── HTTP session (reused for latency) ─────────────────────────────

    async def _ensure_http(self):
        """Lazy-create a shared aiohttp session. Reusing avoids TLS setup per call."""
        if self._http is None:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._http = aiohttp.ClientSession(timeout=timeout)
            logger.debug(f"[Xiaozhi] HTTP session created sid={self.session_id}")
        return self._http

    # ── Cleanup ─────────────────────────────────────────────────────

    async def _cleanup(self) -> None:
        self._interrupt_event.set()
        self._cancel_silence_watchdog()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self._http:
            await self._http.close()
            self._http = None
        logger.info(f"[Xiaozhi] Session cleaned: {self.session_id}")


# ── Opus helpers ───────────────────────────────────────────────

def _opus_decode_one(data: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Decode a single Opus frame to raw PCM s16le.

    Each binary WS frame from the firmware is one Opus frame.
    """
    try:
        import opuslib  # pyright: ignore[reportMissingImports]
        decoder = opuslib.Decoder(sample_rate, channels)
        frame_samples = 60 * sample_rate // 1000  # 60ms frame
        pcm_size = frame_samples * channels * 2
        return decoder.decode(data, pcm_size)
    except ImportError:
        return data
    except Exception:
        return b""


def opus_encode_chunks(
    pcm: bytes, sample_rate: int = 24000, channels: int = 1, frame_duration_ms: int = 60
) -> list[bytes]:
    """Encode PCM s16le to Opus frames (one per frame_duration_ms chunk)."""
    try:
        import opuslib  # pyright: ignore[reportMissingImports]
        encoder = opuslib.Encoder(sample_rate, channels, "voip")
        frames: list[bytes] = []
        frame_samples = frame_duration_ms * sample_rate // 1000
        frame_bytes = frame_samples * channels * 2
        pos = 0
        while pos + frame_bytes <= len(pcm):
            frame = pcm[pos:pos + frame_bytes]
            try:
                encoded = encoder.encode(bytes(frame), frame_samples)
                frames.append(encoded)
            except Exception:
                pass
            pos += frame_bytes
        return frames
    except ImportError:
        logger.warning("[Xiaozhi] opuslib not available for encode")
        return [pcm]


def _make_tts_payload(text: str, voice_id: str, model_id: str) -> dict:
    return {
        "model_id": model_id,
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
        },
        "language": "en",
    }
