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
import tempfile
import time
import uuid
import wave
from collections import deque
from collections.abc import AsyncIterator
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
TTS_MAX_SEND_AHEAD = 1.2   # keep the device decode queue ~1.2s ahead (xiaozhi rate-controller pattern)
# Warm-up gate for the streamed first audio: Cartesia's first chunk is often
# tiny (1-2 frames) and the next chunk can lag 1-2s. Hold the first frames
# until enough audio has accumulated, then release in one go — otherwise the
# device plays 60ms and starves at the very start. Frame-count only: no
# artificial time wait — Cartesia streams at faster-than-realtime, so 5
# frames (300ms) accumulate quickly and the start stays snappy.
TTS_STREAM_WARMUP_FRAMES = 5

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
        self._stt_commit_marked: bool = False
        self._first_turn_done: bool = False
        # Reconstructed end-of-utterance on the server clock (perf_counter),
        # derived from the firmware's user_stop_age_ms duration. None until
        # the first vad_silence arrives.
        self._vad_end_mark: float | None = None
        # C1: ConnectionHandler 持有当前 turn dict 的显式引用,供跨线程
        # 调度 (Deepgram on_message) 的协程在入口处恢复 ContextVar。
        # 避免依赖子线程的 run_coroutine_threadsafe 看到正确的 ctx。
        self._active_turn: dict | None = None
        # M1: 当前正在播放 TTS 的 sentence_id,tts_played 到达时校验
        # 是否对应当前 turn,防止挂错 turn 或晚到丢失。
        self._current_tts_sentence_id: int | None = None
        # N3: buffer for device_playback_ms that arrived AFTER the parent
        # task flushed the turn. The very next tts_played / start of next
        # turn reads this and applies it to the new turn's meta with a
        # `device_playback_ms_late=true` flag, so we never silently lose
        # a device playback delay measurement.
        self._late_tts_played: dict | None = None
        # Device-side playback delay reported by firmware for the current turn
        # (first TTS packet received -> first DAC sample) via tts_played.
        self._device_playback_ms: int = 0
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
        self._last_abort_time: float = 0.0
        # Virtual playback clock for TTS send pacing (xiaozhi AudioRateController pattern)
        self._tts_play_position: float = 0.0
        self._tts_clock_start: float = time.monotonic()

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
        elif state == "tts_played":
            await self._on_tts_played(data)
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
        # C1: 抓住 turn dict 引用,供跨线程回调恢复 ctx
        self._capture_turn_context()
        TelemetryCollector.set_meta(
            "turn_phase",
            "first_after_connect" if not self._first_turn_done else "wake_word",
        )
        TelemetryCollector.mark("detect")

    async def _on_vad_silence(self, data: dict) -> None:
        """Firmware VAD silence — carries a duration, not a timestamp.

        ``user_stop_age_ms`` is how long ago (on the device clock) the AFE VAD
        declared the user stopped speaking. The server reconstructs the true
        end-of-utterance on its own perf_counter clock, which keeps every
        segment of the turn metric on a single time base.

        H3: 同时记录 server_received_vad_at = 实际收到 vad_silence 消息的
        服务器本地 perf_counter。这是另一条 E2E 口径起点 ——
        "服务器看到固件停嘴 → 服务器发出首包音频",不包含上行 RTT。
        旧 vad_end 是"用户停嘴 → 服务器首包",会少算设备到服务器的上行
        网络时延。下游可以基于这两个 mark 选用不同口径。新口径对应的
        segment 名为 server_recv_vad_to_spk (diagnostic)。
        """
        user_stop_age_ms = int(data.get("user_stop_age_ms", 0) or 0)

        # H3: 不管 user_stop_age_ms 是否有效,都记 server_received_vad_at。
        # 这给下游提供一个稳定可对比的"服务器收到停嘴消息"时间锚。
        server_received_at = time.perf_counter()
        if TelemetryCollector.has_mark("vad_start"):
            TelemetryCollector.set_mark("server_received_vad_at", server_received_at)
            # E2E 起点口径选择写到 meta,让下游消费时能识别用的是哪条口径
            TelemetryCollector.set_meta("vad_end_source", "reconstructed_from_age_ms")

        if user_stop_age_ms > 0:
            self._vad_end_mark = server_received_at - user_stop_age_ms / 1000.0
            # Set the authoritative vad_end as soon as the turn is active;
            # if the server-side VAD hasn't started this turn yet, _handle_audio
            # will apply the stored mark lazily.
            if TelemetryCollector.has_mark("vad_start"):
                TelemetryCollector.set_mark("vad_end", self._vad_end_mark)
            logger.info(
                f"[Voice] vad_silence user_stop_age_ms={user_stop_age_ms} "
                f"client={self.client_id}"
            )
        else:
            # 固件没发 user_stop_age_ms 或为 0,vad_end 用 server_received_vad_at 兜底
            self._vad_end_mark = server_received_at
            if TelemetryCollector.has_mark("vad_start"):
                TelemetryCollector.set_mark("vad_end", self._vad_end_mark)
            logger.info(
                f"[Voice] vad_silence (no age_ms, fall back to receive time) "
                f"client={self.client_id}"
            )

    def _flush_late_tts_played(self) -> None:
        """N3: flush a late tts_played into the current turn's meta.

        Called from two places:
        1. At the top of ``_on_tts_played`` — if a tts_played arrives for the
           current turn after the previous one was buffered, write it to the
           current turn with `device_playback_ms_late=true` so the late data
           is preserved instead of dropped.
        2. At the start of every new turn (in ``_on_stt_result``) — absorb
           any device playback delay that arrived after the parent task
           already flushed the previous turn.

        One-slot buffer is enough: we only care about the most recent late
        arrival, since each turn produces at most one tts_played.
        """
        if self._late_tts_played is None:
            return
        ms = self._late_tts_played.get("device_playback_ms")
        sid = self._late_tts_played.get("sentence_id")
        if ms is not None and ms > 0:
            # N3-fix v2: 用独立 key 存 late value,**绝不** 覆盖
            # device_playback_ms(否则新 turn 自己的 tts_played 到达时
            # 会被旧轮的数据覆盖,且 device_playback_ms_late=true 这个标记
            # 永远挂在新 turn meta 上不清掉)
            TelemetryCollector.set_meta("device_playback_ms_late_value", ms)
            TelemetryCollector.set_meta("device_playback_ms_late_sid", sid)
            TelemetryCollector.set_meta("device_playback_ms_late", True)
            logger.info(
                f"[Voice] flushed late tts_played value={ms} "
                f"sid={sid} client={self.client_id}"
            )
        self._late_tts_played = None

    async def _on_tts_played(self, data: dict) -> None:
        """Firmware ack: first TTS packet received -> first DAC write (ms).

        The device sends this once per TTS turn as soon as playback actually
        starts, so the delay is attached to the *current* turn before it is
        finalized at ``tts/stop``.

        M1: validate sentence_id to avoid attaching device_playback_ms to the
        wrong turn (e.g. short reply where device playback ack arrives after
        the next turn has already started).

        N3: if the parent task has already flushed the turn this message was
        for, stash it in ``_late_tts_played`` so the next turn can pick it up
        instead of dropping the data.
        """
        device_playback_ms = int(data.get("device_playback_ms", 0) or 0)
        if device_playback_ms <= 0:
            return

        # M1 + round-3 fix: 校验 sentence_id。三种 buffer 场景:
        # 1. cur_sid is None: 处于 turn 边界(上一个 turn 已 flush,下一个
        #    turn 还没开始),tts_played 是上一轮晚到的,应该 buffer
        # 2. cur_sid is not None, played_sid != cur_sid: sentence_id 不匹配
        # 3. played_sid is None(固件没发或 parse 失败):旧固件路径,无法
        #    校验,直接 set_meta(向后兼容)
        played_sid_raw = data.get("sentence_id")
        played_sid: int | None = None
        if played_sid_raw is not None:
            try:
                played_sid = int(played_sid_raw)
            except (TypeError, ValueError):
                played_sid = None
        cur_sid = self._current_tts_sentence_id
        if played_sid is not None and (
            cur_sid is None or played_sid != cur_sid
        ):
            # cur_sid None 或 mismatch → buffer
            if cur_sid is None:
                logger.warning(
                    f"[Voice] tts_played arrived with no active TTS turn "
                    f"(between turns), buffer for next turn "
                    f"device_playback_ms={device_playback_ms} sid={played_sid} "
                    f"client={self.client_id}"
                )
            else:
                logger.warning(
                    f"[Voice] tts_played sentence_id mismatch: "
                    f"device={played_sid} current={cur_sid}, buffer for next turn "
                    f"device_playback_ms={device_playback_ms} "
                    f"client={self.client_id}"
                )
            self._late_tts_played = {
                "device_playback_ms": device_playback_ms,
                "sentence_id": played_sid,
            }
            return

        # N3: 如果有上一轮晚到的 device_playback_ms,先 flush 到当前 turn
        # meta,带 late 标记
        self._flush_late_tts_played()
        # M1: device_playback_ms 仅作为 meta 写入当前 turn, _device_playback_ms
        # 实例属性目前没有读路径,属于死字段,不再写。
        TelemetryCollector.set_meta("device_playback_ms", device_playback_ms)
        logger.info(
            f"[Voice] tts_played device_playback_ms={device_playback_ms} "
            f"client={self.client_id}"
        )

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

        # Barge-in is handled by Deepgram on_message: any short interim speech
        # while TTS is playing → _on_interim_barge_in() triggers abort.
        # VAD here only manages voice state for turn tracking; it does NOT
        # trigger barge-in directly — avoids false positives from noise.

        # Cancel EOU bounce if user resumes speaking (LiveKit pattern)
        if have_voice and self._pending_speech_final:
            logger.info("[Voice] EOU bounce cancelled — user resumed speaking")
            self._pending_speech_final = ""
            if self._eou_bounce_task and not self._eou_bounce_task.done():
                self._eou_bounce_task.cancel()

        # Latency marks: vad_start + stt_commit from the server VAD state
        # transitions. The authoritative vad_end (user stopped speaking) is
        # reconstructed separately from the firmware vad_silence duration.
        # Lazily start turn for follow-up utterances (wake word started in _on_detect).
        if self.client_have_voice and not self._vad_start_marked:
            if getattr(self, "_turn_type", "follow_up") != "wake_word":
                TelemetryCollector.start_turn(
                    user_id=self._user_id or self.client_id,
                    persona_id=self._persona_id,
                )
                # C1: 抓住 turn dict 引用
                self._capture_turn_context()
                TelemetryCollector.set_meta("turn_phase", "follow_up")
            TelemetryCollector.mark("vad_start")
            self._vad_start_marked = True
            # A firmware vad_silence may have arrived before the server-side
            # VAD confirmed speech — apply the reconstructed vad_end lazily.
            if self._vad_end_mark is not None:
                TelemetryCollector.set_mark("vad_end", self._vad_end_mark)
        if self.client_voice_stop and not self._stt_commit_marked:
            # Server-side Silero VAD confirms end-of-utterance. This is the
            # "EOU detection delay" segment, not the authoritative user-stop.
            TelemetryCollector.mark("stt_commit")
            self._stt_commit_marked = True

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

    # ── Cross-thread turn context (C1 fix) ────────────────────────
    #
    # Background: Deepgram's on_message runs in a background thread. When it
    # calls `asyncio.run_coroutine_threadsafe(coro, conn._loop)`, the
    # coroutine is scheduled into the main asyncio loop, but it runs in the
    # **default context** — WebSocket handler task's turn dict is invisible.
    # Result: every TelemetryCollector.mark(...) inside that coroutine
    # becomes a no-op, and the turn never gets E2E / segments logged.
    #
    # Fix pattern (two ends):
    #   1. After `TelemetryCollector.start_turn(...)` in the asyncio task
    #      that owns the turn, call `_capture_turn_context()` to save the
    #      dict reference to `self._active_turn`.
    #   2. At the **top of every coroutine that may be dispatched from a
    #      non-asyncio thread** (Deepgram thread, future STT providers,
    #      webhook callbacks, etc.), call `_restore_turn_context()` to
    #      re-bind the ContextVar before any mark/finish_turn call.
    #
    # ⚠ If you add a new cross-thread entry point, call
    # `_restore_turn_context()` FIRST, before any other mark. Forgetting
    # this is a **silent** bug: marks become no-ops, the turn dict never
    # accumulates marks, and the E2E never lands in the DB. Always pair
    # the new entry with an explicit call here.

    def _restore_turn_context(self) -> None:
        """Re-bind the saved turn dict into the current ContextVar.

        C1 fix, entry side. Call at the top of any coroutine dispatched
        from another thread (Deepgram on_message, etc.) before any
        TelemetryCollector call. Idempotent and cheap: O(1) dict ref set.
        """
        if self._active_turn is not None:
            from metrics.turn import _current_var as _turn_var
            _turn_var.set(self._active_turn)

    def _capture_turn_context(self) -> None:
        """Snapshot the active turn dict for cross-thread callbacks.

        C1 fix, dispatch side. Call from the asyncio task that owns the
        turn, immediately after `TelemetryCollector.start_turn(...)`.
        Stores the dict reference in `self._active_turn` so the
        cross-thread entry coroutine can re-bind it via
        `_restore_turn_context()`.
        """
        from metrics.turn import _current_var as _turn_var
        self._active_turn = _turn_var.get()

    async def _on_stt_final(self, text: str) -> None:
        """Deepgram speech_final — start EOU bounce timer before committing.
        C1: 这是从 Deepgram 线程通过 run_coroutine_threadsafe 调度的协程,
        入口必须显式恢复 turn context,否则 mark 都打不上。"""
        self._restore_turn_context()
        logger.info(f"[Voice] STT final (bounce {self._eou_bounce_delay*1000:.0f}ms): '{text[:200]}'")
        self._pending_speech_final = text.strip()
        # Cancel any previous bounce
        if self._eou_bounce_task and not self._eou_bounce_task.done():
            self._eou_bounce_task.cancel()
        self._eou_bounce_task = asyncio.ensure_future(self._eou_bounce())

    async def _on_interim_barge_in(self) -> None:
        """Pipecat-style: Deepgram interim speech during TTS → abort immediately."""
        now = time.time()
        if now - self._last_abort_time < 1.0:
            return  # abort in flight — ignore repeats from queued interims
        self._last_abort_time = now
        logger.info("[Voice] Barge-in abort")
        self._reset_tts_clock()
        self._interrupt_event.set()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        await self._ws.send(json.dumps({
            "session_id": self.session_id,
            "type": "tts",
            "state": "abort",
        }))
        self.client_is_speaking = False

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
        self._stt_commit_marked = False
        self._vad_end_mark = None
        self._pending_speech_final = ""
        if self._eou_bounce_task and not self._eou_bounce_task.done():
            self._eou_bounce_task.cancel()

    # ── Silence watchdog ──────────────────────────────────────────────

    # ── STT result → LLM → TTS (official: speech_to_text_wrapper → startToChat → chat) ──

    async def _on_stt_result(self, text: str) -> None:
        """Handle STT result: send to client, persist, launch LLM."""
        # C1: 入口恢复 turn context(可能被 Deepgram 线程或 _eou_bounce 调度)
        self._restore_turn_context()
        TelemetryCollector.mark("stt_final")
        # Authoritative vad_end normally comes from the firmware's local VAD
        # (user_stop_age_ms). If that path is unavailable — e.g. the device is
        # running with a reference channel / device-side AEC, where the AFE VAD
        # is intentionally disabled — fall back to the server Silero VAD
        # (stt_commit) first, and only as a last resort use the wake-word
        # instant (detect). H1: detect is "wake word moment", not "user
        # stopped speaking"; using it as vad_end inflates E2E by the entire
        # wake-to-stop interval and breaks consistency with follow-up turns.
        if not TelemetryCollector.has_mark("vad_end"):
            if self._vad_end_mark is not None:
                # H3: 走 user_stop_age_ms 重建路径
                TelemetryCollector.set_mark("vad_end", self._vad_end_mark)
                TelemetryCollector.set_meta("vad_end_source", "reconstructed_from_age_ms")
            else:
                stt_commit_time = TelemetryCollector.mark_time("stt_commit")
                if stt_commit_time is not None:
                    # H1 fallback: 用服务端 Silero VAD
                    TelemetryCollector.set_mark("vad_end", stt_commit_time)
                    TelemetryCollector.set_meta("vad_end_fallback", "stt_commit")
                    TelemetryCollector.set_meta("vad_end_source", "stt_commit_fallback")
                else:
                    detect_time = TelemetryCollector.mark_time("detect")
                    if detect_time is not None:
                        # H1 fallback: 没有 stt_commit 只能用 detect 时,在
                        # meta 里显式标注,wake-word turn 的 E2E 数字会偏高,
                        # 需要在下游消费时单独识别。
                        TelemetryCollector.set_mark("vad_end", detect_time)
                        TelemetryCollector.set_meta("vad_end_fallback", "detect")
                        TelemetryCollector.set_meta("vad_end_source", "detect_fallback")
                    else:
                        TelemetryCollector.set_meta("vad_end_fallback", "none")
                        TelemetryCollector.set_meta("vad_end_source", "none")

        # If TTS is still playing, skip — barge-in (Deepgram interim) handles
        # interruption. If an LLM/TTS task is still in progress but TTS hasn't
        # started yet (LLM generation phase), cancel it before launching a new one.
        if self.client_is_speaking:
            logger.info(f"[Voice] Skipping STT (TTS still playing): '{text[:60]}'")
            return
        if self._tts_task and not self._tts_task.done():
            logger.info(f"[Voice] Cancelling previous TTS task for new turn")
            self._tts_task.cancel()

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
            TelemetryCollector.mark("agent_init")
            self._first_turn_done = True

        # Persist user message (must be after _pig creation)
        if self._pig and self._pig.ctx:
            asyncio.ensure_future(self._persist_turn("user", text))

        # H3: clear interrupt before launching new LLM, after old TTS has yielded
        self._interrupt_event.clear()

        sentence_id = self._sentence_id
        self._sentence_id += 1

        # N3: 新一轮 turn 启动,如果有上一轮晚到的 device_playback_ms,
        # 先写到这一轮 meta(带 late 标记),再开新 tts_task
        self._flush_late_tts_played()
        # M1: 记录当前正在播放 TTS 的 sentence_id,tts_played 到达时校验
        self._current_tts_sentence_id = sentence_id

        # LLM → TTS
        self._tts_task = asyncio.ensure_future(
            self._tts_producer_consumer(text, sentence_id)
        )

        # H5: 等子 task 完成后,由父 task 真正 flush turn。
        # 子 task 内部已调 finish_turn 标 _finished,这里只 _flush_turn
        # (log + 清 ctx)。如果子 task 抛错被取消,也要保证 ctx 被清。
        try:
            await self._tts_task
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            TelemetryCollector._flush_turn()

    def _save_input_wav(self, stt_text: str) -> str:
        """Save user speech PCM as WAV. Returns the file path."""
        try:
            pcm = b"".join(self.asr_audio)
            sid = getattr(self, "_sentence_id", 0)
            ts = int(time.time() * 1000)  # prevent overwrite from duplicate saves
            wav_path = f"/tmp/pigugu_in_{self.session_id}_{sid}_{ts}.wav"
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

    async def _send_tts_frames(self, frames, extra_break=None) -> None:
        """Send frames paced by a virtual playback clock (xiaozhi
        AudioRateController pattern), aborting on interrupt.

        Frames may run up to TTS_MAX_SEND_AHEAD ahead of real time so the
        device's decode queue stays prefilled. Its software AEC reference
        resets after 100 ms without playback, so TTS synthesis gaps between
        sentences must never starve the queue. Waits are computed against
        the clock — no per-frame sleep drift. A negative lead (long stall)
        re-anchors the clock so the refill burst is capped at
        TTS_MAX_SEND_AHEAD.
        """
        for frame in frames:
            if self._interrupt_event.is_set() or (extra_break and extra_break()):
                break
            await self._ws.send(frame)
            self._tts_play_position += TTS_FRAME_INTERVAL
            lead = self._tts_play_position - (time.monotonic() - self._tts_clock_start)
            if lead < 0:
                # Fell behind real time — the send side (loop stall or socket
                # backpressure) couldn't keep the 60ms cadence. Diagnostic for
                # locating mid-play stutter.
                if lead < -0.3:
                    logger.warning(f"[Voice] send fell behind {lead:.2f}s — re-anchoring")
                self._tts_clock_start = time.monotonic() - self._tts_play_position
                lead = 0.0
            if lead > TTS_MAX_SEND_AHEAD:
                await asyncio.sleep(lead - TTS_MAX_SEND_AHEAD)

    async def _wait_playback_drain(self) -> bool:
        """Wait until queued TTS audio has finished playing on the device
        (xiaozhi _wait_for_audio_completion pattern) — send "stop" only when
        the device is actually done, keeping protocol state and audible
        playback aligned. Returns True if playback drained naturally, False
        if interrupted (abort already sent)."""
        while True:
            lead = self._tts_play_position - (time.monotonic() - self._tts_clock_start)
            if lead <= 0:
                return True
            try:
                await asyncio.wait_for(self._interrupt_event.wait(), timeout=lead)
                return False
            except asyncio.TimeoutError:
                pass

    def _reset_tts_clock(self) -> None:
        """Re-anchor the playback clock after an abort flushed the device
        queue — otherwise the stale lead would delay the next turn's first
        frames by up to TTS_MAX_SEND_AHEAD."""
        self._tts_play_position = 0.0
        self._tts_clock_start = time.monotonic()

    async def _tts_producer_consumer(self, stt_text: str, sentence_id: int) -> None:
        """LLM → Cartesia WS streaming TTS → paced send (xiaozhi rate
        controller). Text flows straight from the LLM into Cartesia
        (continue_=True keeps prosody across chunks); audio is sent through
        the virtual playback clock as it arrives. No text segmentation, no
        per-segment synthesis — the device never starves between segments.
        On stream failure the turn degrades to one whole-reply REST
        synthesis."""
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        pig = self._pig
        if pig is None:
            return
        tts_pcm = bytearray()  # collect raw PCM for debug WAV

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
                    if isinstance(chunk, str):
                        await text_queue.put(chunk)
                        full += chunk
                logger.info(f"[Voice] LLM producer done: {len(full)} chars")
            except Exception:
                logger.exception("[Voice] LLM producer failed")
            finally:
                await text_queue.put(None)
            return full

        async def _iter_text() -> AsyncIterator[str]:
            while True:
                chunk = await text_queue.get()
                if chunk is None:
                    return
                yield chunk

        async def _send_start() -> None:
            # M1: tts/start 带 sentence_id,设备 tts_played 回带,
            # 服务端按序号写入对应 turn,避免晚到/挂错。
            # 注意:用 self._sentence_id 已经被 _on_stst_result 加过 1,
            # 这里我们用 _current_tts_sentence_id 拿到的就是正在播放的
            # 那一句(在 _on_stst_result 里同步设置过)。
            sid = self._current_tts_sentence_id
            payload = {
                "session_id": self.session_id,
                "type": "tts",
                "state": "start",
            }
            if sid is not None and sid > 0:
                payload["sentence_id"] = sid
            await self._ws.send(json.dumps(payload))
            self.client_is_speaking = True
            TelemetryCollector.mark("tts_start")

        async def _mark_first_tts() -> None:
            elapsed = time.time() - send_phase_start
            logger.info(f"[Voice] ⏱ First TTS sent: +{elapsed:.2f}s (stream)")
            TelemetryCollector.mark("tts_first_ready")
            TelemetryCollector.mark("agent_spk")

        async def _send_batch(frames: list[bytes], *, first: bool = False) -> None:
            nonlocal tts_started
            if self._interrupt_event.is_set() or not frames:
                return
            if not tts_started:
                await _send_start()
                tts_started = True
            if first:
                await _mark_first_tts()
            await self._send_tts_frames(frames)

        producer_task = asyncio.ensure_future(_producer())
        tts_started = False
        send_phase_start = time.time()

        try:
            try:
                pending: list[bytes] = []
                async for frames in self.tts.stream_audio(
                    _iter_text(), self._interrupt_event, collect_pcm=tts_pcm
                ):
                    if self._interrupt_event.is_set():
                        break
                    if not tts_started:
                        # Warm-up gate: accumulate the first frames so a tiny
                        # first chunk + slow next chunk doesn't stutter the
                        # start of playback.
                        pending.extend(frames)
                        if len(pending) >= TTS_STREAM_WARMUP_FRAMES:
                            # Re-anchor the clock here so the warm-up wait
                            # isn't counted as the send side falling behind.
                            self._reset_tts_clock()
                            await _send_start()
                            tts_started = True
                            await _mark_first_tts()
                            await self._send_tts_frames(pending)
                            pending = []
                    else:
                        await _send_batch(frames)
                # Stream ended while still warming up (very short reply):
                # release whatever audio accumulated.
                if not tts_started and pending and not self._interrupt_event.is_set():
                    await _send_start()
                    tts_started = True
                    await _mark_first_tts()
                    await self._send_tts_frames(pending)
            except RuntimeError:
                # Degraded mode: one whole-reply REST synthesis. Only when
                # nothing has been played yet — a mid-stream failure must not
                # replay the whole reply over already-played audio.
                if tts_started:
                    logger.warning("[Voice] WS stream died mid-turn — ending turn")
                elif not self._interrupt_event.is_set():
                    logger.warning("[Voice] WS stream failed — falling back to REST")
                    full = await producer_task
                    if full.strip() and self.tts:
                        frames = await self.tts.synthesize(full.strip(), collect_pcm=tts_pcm)
                        await _send_batch(frames, first=True)
        except asyncio.CancelledError:
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        finally:
            self.client_is_speaking = False

        if not producer_task.done():
            full_text = await producer_task
        else:
            try:
                full_text = producer_task.result()
            except (asyncio.CancelledError, Exception):
                full_text = ""

        # Save TTS output WAV for debugging
        if len(tts_pcm) > 0:
            self._save_tts_wav(bytes(tts_pcm))

        if tts_started:
            if await self._wait_playback_drain() and not self._interrupt_event.is_set():
                await self._ws.send(json.dumps({
                    "session_id": self.session_id,
                    "type": "tts",
                    "state": "stop",
                }))
                TelemetryCollector.mark("tts_end")
            # H5: 不在这里 finish_turn。子 task 调 finish_turn 只能 mark
            # _finished,真正 flush (_log + 清 ctx) 由父 task 在 await
            # tts_task 之后做。这里只 mark _finished。
            TelemetryCollector.finish_turn()
            # M1: TTS 结束,清掉当前 sentence_id 跟踪
            self._current_tts_sentence_id = None

        # Persist assistant message
        if full_text.strip() and self._pig and self._pig.ctx:
            asyncio.ensure_future(self._persist_turn("assistant", full_text.strip()))
    # ── Abort ─────────────────────────────────────────────────────────

    async def _handle_abort(self, data: dict) -> None:
        reason = data.get("reason", "unknown")
        logger.info(f"[Voice] Abort reason={reason}")
        self._sentence_id += 1
        self._reset_tts_clock()
        self._interrupt_event.set()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        self.client_is_speaking = False
        # M1: abort (not stop) so firmware flushes decode queue
        await self._ws.send(json.dumps({
            "session_id": self.session_id,
            "type": "tts",
            "state": "abort",
        }))
        # H1: clear interrupt so next turn's LLM can run
        self._interrupt_event.clear()

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
            was_speaking = self.client_is_speaking
            if self._tts_task and not self._tts_task.done():
                self._interrupt_event.set()
                self._tts_task.cancel()
                try:
                    await self._tts_task
                except asyncio.CancelledError:
                    # Old task's cleanup (client_is_speaking=False) has run by
                    # now — must precede our True write below, else a late
                    # cleanup could clobber the flag mid-inject.
                    pass
            self._interrupt_event.clear()
            if was_speaking:
                # The device may still hold up to TTS_MAX_SEND_AHEAD of queued
                # audio — flush it so the inject starts promptly.
                self._reset_tts_clock()
                await self._ws.send(json.dumps({
                    "session_id": self.session_id,
                    "type": "tts",
                    "state": "abort",
                }))
            await self._ws.send(json.dumps({
                "session_id": self.session_id,
                "type": "tts",
                "state": "start",
            }))
            self.client_is_speaking = True
            self._tts_task = asyncio.ensure_future(
                self._inject_tts(msg, sentence_id)
            )
            try:
                await self._tts_task
            except asyncio.CancelledError:
                logger.info("[Voice] Inject cancelled by new turn")

    async def _inject_tts(self, msg: dict, sentence_id: int) -> None:
        try:
            text = msg.get("text", msg.get("content", msg.get("prompt", "")))
            if self.tts and text:
                tts_pcm = bytearray()
                frames = await self.tts.synthesize(text, collect_pcm=tts_pcm)
                await self._send_tts_frames(
                    frames, extra_break=lambda: sentence_id != self._sentence_id
                )
                if len(tts_pcm) > 0:
                    self._save_tts_wav(bytes(tts_pcm))
            if await self._wait_playback_drain() and not self._interrupt_event.is_set():
                await self._ws.send(json.dumps({
                    "session_id": self.session_id,
                    "type": "tts",
                    "state": "stop",
                }))
        except Exception:
            logger.exception("[Voice] Inject TTS failed")
        finally:
            self.client_is_speaking = False

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
