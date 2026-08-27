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
from voice.interims import InterimBuffer
from voice.storage import TurnStorage, is_turn_storage_enabled

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


def _ms_diff(a: float | None, b: float | None) -> int | None:
    """Difference between two perf_counter floats, in milliseconds,
    rounded to int. Returns None if either side is missing or 0 if
    b < a (negative span — caller should fall back to a wider window
    or just record 0)."""
    if a is None or b is None:
        return None
    if b < a:
        return 0
    return round((b - a) * 1000.0)


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
        # ── Per-turn audio storage (S3 + ClickHouse) ──────────────
        # New path: when ENABLE_TURN_STORAGE=true (default), we build a
        # 5-file per-turn record (input.wav/json, tts.wav/json,
        # turn.json) and INSERT one row into voice.turns. The legacy
        # /tmp/pigugu_*.wav path is kept as a fallback for when the
        # new subsystem is disabled.
        self._turn_storage_enabled: bool = is_turn_storage_enabled()
        # Per-session turn counter (separate from _sentence_id which
        # is the per-TTS-sentence counter used for tts_played
        # correlation). 1-based; the {turn_idx:04d} in turn_id.
        self._turn_idx: int = 0
        # Live TurnStorage instance for the in-flight turn, or None
        # between turns. Created in _on_stt_result, committed in the
        # parent task after TTS completes.
        self._turn_storage: TurnStorage | None = None
        # Set to _turn_idx when a commit() is fired for that turn. The
        # _save_input_wav cleanup path uses this to skip creating a
        # PHANTOM second commit for the same user audio buffer
        # (otherwise: asr_audio still has the turn's PCM, the cleanup
        # path builds a new TurnStorage with the same data but
        # turn_idx+1, and we get two S3 directories + two CH rows for
        # the same turn). Cleared in the commit's done callback.
        self._turn_storage_committing_turn_idx: int = -1
        # Index into conn._voice_chunk_flags (populated by Silero) at
        # the moment the current turn started. Captured at
        # _on_stt_result so commit() can slice the per-turn range.
        self._voice_chunk_start: int = 0
        # Cross-thread interim STT buffer. Shared across all turns in
        # this connection; the TurnStorage drains it on each
        # mark_stt_final / mark_stt_abandoned call.
        self._interim_buffer = InterimBuffer()
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

    async def _on_stt_interim(self, text: str) -> None:
        """Deepgram interim message — append to the per-turn interim buffer.

        Dispatched from the Deepgram background thread via
        ``asyncio.run_coroutine_threadsafe``; the InterimBuffer itself
        is thread-safe so the dispatch is purely for ordering with
        other asyncio mutations.

        The interim is only meaningful while a turn is in flight; we
        accept it whenever the buffer is alive (the active TurnStorage
        drains it on mark_stt_final / mark_stt_abandoned).
        """
        if not self._turn_storage_enabled:
            return
        self._interim_buffer.record(text)

    def _voice_chunk_flags_slice(self, start_idx: int) -> list[bool]:
        """Closure passed to TurnStorage: returns the per-turn slice
        of the Silero chunk-flag list. ``start_idx`` is captured at
        TurnStorage construction time so the next turn's
        ``_voice_chunk_start`` mutation cannot leak into this turn's
        slice.

        ``start_idx`` is in the ORIGINAL coordinate space (before any
        trim by Silero's 10-minute bound). The trim counter
        ``_voice_chunk_flags_trimmed`` tracks how many chunks have
        been removed from the front of the list; we subtract it
        to translate to the post-trim coordinate space. Without
        this translation, a long session (>= 10 min cumulative
        audio) loses the first ~60s of every subsequent turn's
        voice_segments[].
        """
        flags = getattr(self, "_voice_chunk_flags", None)
        if not flags:
            return []
        trimmed = getattr(self, "_voice_chunk_flags_trimmed", 0)
        # Translate to post-trim coordinates. If the translation
        # yields a negative index (turn started before any trim —
        # common in the first 10 minutes of a session), clamp to 0.
        post_trim_idx = max(0, start_idx - trimmed)
        return list(flags[post_trim_idx:])

    def _make_turn_storage(
        self,
        *,
        turn_id: str,
        utc_start_ms: int,
    ) -> TurnStorage | None:
        """Build a TurnStorage for the in-flight turn. Returns None
        when the env-var configuration is missing or incomplete (in
        which case the agent logs a one-shot ERROR and falls back to
        the legacy /tmp path for this connection)."""
        bucket = os.getenv("AUDIO_S3_BUCKET", "").strip()
        prefix = os.getenv("AUDIO_S3_PREFIX", "voice-turns").strip()
        ch_url = os.getenv("CLICKHOUSE_URL", "").strip()
        ch_db = os.getenv("CLICKHOUSE_DATABASE", "voice").strip()
        ch_table = os.getenv("CLICKHOUSE_TABLE", f"{ch_db}.turns").strip()
        ch_password = os.getenv("CLICKHOUSE_PASSWORD", "")
        if not (bucket and ch_url and ch_password):
            logger.warning(
                f"[Voice] turn storage misconfigured "
                f"bucket={bucket!r} ch_url={ch_url!r} ch_password_set={bool(ch_password)}"
            )
            return None
        # asynch accepts the URL form `http://host:port?password=...`.
        ch_dsn = f"{ch_url}?password={ch_password}"
        # Capture the start index in a local so the lambda is bound to
        # THIS turn's value, not the live self._voice_chunk_start
        # (which the next turn will overwrite before this turn's
        # commit() runs — the S3+CH I/O can take seconds).
        captured_start = self._voice_chunk_start
        return TurnStorage(
            turn_id=turn_id,
            session_id=self.session_id,
            turn_idx=self._turn_idx,
            device_id=self.client_id,
            user_id=self._user_id or self.client_id,
            persona_id=self._persona_id,
            utc_start_ms=utc_start_ms,
            s3_bucket=bucket,
            s3_prefix=prefix,
            clickhouse_dsn=ch_dsn,
            clickhouse_table=ch_table,
            interims=self._interim_buffer,
            voice_chunk_flags_slice=lambda: self._voice_chunk_flags_slice(captured_start),
            turn_type=self._turn_type,
        )

    async def _commit_turn_storage(self, *, turn_type_override: str = "") -> None:
        """Commit (or no-op) the in-flight TurnStorage. Fire-and-forget;
        failures are logged by the storage module but never propagate
        to the WebSocket loop.
        """
        if not self._turn_storage_enabled or self._turn_storage is None:
            return
        storage = self._turn_storage
        self._turn_storage = None
        if turn_type_override:
            storage.set_turn_type(turn_type_override)
        # Snapshot the current latency state from TelemetryCollector so
        # the sidecar has it. TelemetryCollector is a contextvar on
        # this task; reading here is safe.
        from metrics.turn import _current_var as _turn_var
        turn = _turn_var.get() or {}
        marks = turn.get("marks", {}) or {}
        e2e_ms = _ms_diff(marks.get("server_received_vad_at"), marks.get("agent_spk"))
        if e2e_ms is None:
            e2e_ms = _ms_diff(marks.get("vad_end"), marks.get("agent_spk")) or 0
        stt_ms = _ms_diff(marks.get("server_received_vad_at"), marks.get("stt_final")) or 0
        llm_ttft_ms = _ms_diff(marks.get("llm_req"), marks.get("llm_first_token")) or 0
        tts_ttfb_ms = _ms_diff(marks.get("tts_first_ready"), marks.get("agent_spk")) or 0
        device_playback_ms = int(
            (turn.get("meta") or {}).get("device_playback_ms", 0) or 0
        )
        llm_model = (turn.get("meta") or {}).get("llm_model", "")
        storage.set_telemetry({
            "e2e_ms": e2e_ms,
            "stt_ms": stt_ms,
            "llm_ttft_ms": llm_ttft_ms,
            "tts_ttfb_ms": tts_ttfb_ms,
            "device_playback_ms": device_playback_ms,
            "llm_model": llm_model,
        })
        # Best-effort: never await the S3+CH I/O synchronously.
        # Mark this turn_idx as "commit in flight" so the
        # _save_input_wav cleanup path doesn't build a phantom second
        # commit with the same asr_audio buffer. The flag is cleared
        # by the commit's done callback (success or failure).
        self._turn_storage_committing_turn_idx = storage.turn_idx
        commit_task = asyncio.ensure_future(storage.commit())

        def _on_commit_done(t: asyncio.Task) -> None:
            # Only clear the flag if it still refers to OUR turn. If
            # the next turn's commit already overwrote it, leave it.
            if self._turn_storage_committing_turn_idx == storage.turn_idx:
                self._turn_storage_committing_turn_idx = -1
            # Surface unhandled exceptions (commit() catches its own,
            # but defensively log anything that escaped).
            if not t.cancelled() and t.exception() is not None:
                logger.exception(
                    f"[Voice] commit() escaped exception turn_id={storage.turn_id} "
                    f"err={t.exception()!r}"
                )
        commit_task.add_done_callback(_on_commit_done)

    async def _commit_turn_storage_async(self, *, turn_type_override: str = "") -> None:
        """Sync-callable wrapper around ``_commit_turn_storage`` for
        legacy call sites that fire-and-forget (the cleanup path
        inside ``_save_input_wav``)."""
        await self._commit_turn_storage(turn_type_override=turn_type_override)

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

        # ── Per-turn audio storage: build the TurnStorage ─────────
        # Capture the current Silero chunk index BEFORE the new turn
        # starts, so commit() can slice the per-turn range of flags
        # for voice_segments[].
        if self._turn_storage_enabled:
            self._turn_idx += 1
            self._voice_chunk_start = len(
                getattr(self, "_voice_chunk_flags", [])
            )
            utc_start_ms = int(time.time() * 1000)
            turn_id = (
                f"{utc_start_ms}_{self.session_id}_{self._turn_idx:04d}"
            )
            self._turn_storage = self._make_turn_storage(
                turn_id=turn_id,
                utc_start_ms=utc_start_ms,
            )
            if self._turn_storage is not None:
                # Freeze the user PCM at STT final time so a future
                # ``asr_audio.clear()`` doesn't lose it.
                self._turn_storage.set_user_pcm(b"".join(self.asr_audio))
                self._turn_storage.mark_stt_final(text)
                # Telemetry snapshot (e2e_ms etc. are computed at flush,
                # but other fields are already populated).
                llm_model = TelemetryCollector.mark_time("llm_model")  # type: ignore[arg-type]
                snapshot: dict[str, Any] = {
                    "llm_model": self._pig.model if self._pig else "",
                }
                self._turn_storage.set_telemetry(snapshot)

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
            # Commit the per-turn audio record (S3 + ClickHouse).
            # Best-effort, never blocks this coroutine.
            await self._commit_turn_storage()

    def _save_input_wav(self, stt_text: str) -> str:
        """Legacy /tmp WAV save for the user PCM. Kept as a fallback
        when ``ENABLE_TURN_STORAGE=false`` (or when the new
        TurnStorage is None because env vars are missing). When the
        new path is active, the TurnStorage has already captured the
        PCM at STT final time in ``_on_stt_result`` — this is a
        no-op for that case, except for the cleanup path which
        builds a placeholder TurnStorage and commits it.

        Returns the local file path (empty string if the new path
        was used or if the save failed)."""
        # New path: if a TurnStorage exists for this turn, no work
        # to do here — the parent task will commit at the end of
        # _on_stt_result.
        if self._turn_storage_enabled and self._turn_storage is not None:
            return ""
        # New path: a commit is already in flight for this turn (the
        # parent task fired commit() but _save_input_wav is called
        # before the commit completes from _reset_audio_states). The
        # same PCM will be in S3 + CH; do NOT also write a duplicate
        # to /tmp.
        if (
            self._turn_storage_enabled
            and self._turn_storage_committing_turn_idx == self._turn_idx
        ):
            return ""
        # Cleanup path: no STT was produced (the turn ended without
        # a final). Try to spin up a best-effort TurnStorage — BUT
        # skip if a commit is already in flight for the CURRENT turn
        # index. The parent task calls _commit_turn_storage which
        # sets _turn_storage = None but fires commit() asynchronously
        # (S3 + CH I/O takes seconds). The asr_audio buffer is NOT
        # cleared until _reset_audio_states runs, so without this
        # guard we'd build a SECOND TurnStorage with the same PCM,
        # producing two S3 directories + two CH rows for one turn.
        if (
            self._turn_storage_enabled
            and self.asr_audio
            and self._turn_storage_committing_turn_idx != self._turn_idx
        ):
            try:
                self._turn_idx += 1
                utc_start_ms = int(time.time() * 1000)
                turn_id = (
                    f"{utc_start_ms}_{self.session_id}_{self._turn_idx:04d}"
                )
                # We don't know vad_start here; the chunk slice
                # closure is best-effort and may include the previous
                # turn's last segment. Acceptable for the no-STT
                # diagnostic case.
                self._voice_chunk_start = 0
                storage = self._make_turn_storage(
                    turn_id=turn_id,
                    utc_start_ms=utc_start_ms,
                )
                if storage is not None:
                    storage.set_turn_type("interrupted")
                    storage.set_user_pcm(b"".join(self.asr_audio))
                    storage.mark_stt_final("")  # sets stt_status=no_stt
                    self._turn_storage = storage
                    # Schedule the commit; mirror the TTS-end path.
                    asyncio.ensure_future(
                        self._commit_turn_storage_async(turn_type_override="interrupted")
                    )
                    return ""
            except Exception:
                logger.exception("[Voice] best-effort TurnStorage failed")
        # Legacy /tmp path (used when ENABLE_TURN_STORAGE=false or
        # when the new path failed to construct).
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
        """Legacy /tmp WAV save for the TTS PCM. When the new
        TurnStorage path is active, appends to the per-turn TTS
        buffer instead; the actual S3 upload happens in commit().

        The signature preserves the existing call sites
        (``_tts_producer_consumer`` and ``_inject_tts``), which still
        pass the captured PCM through this method. Returns the local
        file path for the legacy path; empty string for the new
        path."""
        # New path: append to the TurnStorage's tts_pcm_buf.
        if self._turn_storage_enabled and self._turn_storage is not None:
            self._turn_storage.tts_pcm_buf.extend(tts_pcm)
            return ""
        # Legacy /tmp path.
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

    def _save_inject_tts_wav(self, tts_pcm: bytes, sentence_id: int) -> str:
        """Diagnostic save for an inject's TTS PCM. Always writes to
        a /tmp file with a per-inject suffix — never appends to the
        active TurnStorage's tts_pcm_buf, which would leak the
        inject's audio into the next real turn's S3 commit.
        """
        try:
            ts = int(time.time() * 1000)
            wav_path = f"/tmp/pigugu_inject_out_{self.session_id}_{sentence_id}_{ts}.wav"
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(tts_pcm)
            logger.warning(
                f"[Voice] Inject TTS WAV saved: {wav_path} "
                f"({len(tts_pcm)} bytes, {len(tts_pcm)/32000:.1f}s)"
            )
            return wav_path
        except Exception:
            logger.exception("[Voice] Inject TTS WAV save failed")
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

        # State flags set inside the streaming try/except and read
        # afterwards. Declared BEFORE the outer try so the inner
        # except RuntimeError block can set stream_failed_mid_turn
        # without an UnboundLocalError and so the post-stream tail
        # can read the value that survived a normal exit of the
        # streaming try.
        stream_failed_mid_turn = False
        cancelled_during_stream = False
        cancel_exc: asyncio.CancelledError | None = None
        # Set to True once the if tts_started block has called
        # mark_tts_complete. The tail's except Exception handler
        # must not overwrite a clean tts_status with a misleading
        # "stream_failed" once bookkeeping has already run.
        bookkeeping_done = False

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
                    # The device already received some audio but the
                    # rest of the stream blew up. Don't fall through
                    # to the normal drain + stop path — that would
                    # claim the turn is "complete" when it isn't.
                    stream_failed_mid_turn = True
                elif not self._interrupt_event.is_set():
                    logger.warning("[Voice] WS stream failed — falling back to REST")
                    full = await producer_task
                    if full.strip() and self.tts:
                        try:
                            frames = await self.tts.synthesize(full.strip(), collect_pcm=tts_pcm)
                        except Exception:
                            # The REST fallback itself blew up (e.g.
                            # ConnectionError). The device still
                            # received nothing, so this is functionally
                            # the same as the LLM returning empty —
                            # let the post-stream tail record an empty
                            # turn (no_tts_started), not stream_failed
                            # (which would imply the device heard a
                            # partial stream).
                            logger.exception(
                                "[Voice] REST fallback synthesize failed"
                            )
                        else:
                            if frames:
                                await _send_batch(frames, first=True)
            except Exception:
                # Anything that isn't a RuntimeError (e.g.
                # websockets.exceptions.ConnectionClosed,
                # WebSocketException, KeyError, ValueError) from
                # stream_audio, _send_tts_frames, or _send_batch.
                # If we've already sent any frames to the device,
                # treat this as a partial stream — the device
                # received a prefix of the audio but no clean end.
                # If nothing reached the device yet, swallow (don't
                # re-raise) so the post-stream tail still runs and
                # writes the no_tts_started outcome to TurnStorage.
                if tts_started:
                    logger.warning(
                        "[Voice] WS stream raised non-RuntimeError mid-turn — ending turn"
                    )
                    stream_failed_mid_turn = True
                else:
                    # Nothing reached the device. The parent's
                    # `except (CancelledError, Exception): pass`
                    # would have hidden this anyway; let the
                    # post-stream tail run and record an empty turn.
                    logger.warning(
                        "[Voice] WS stream raised non-RuntimeError before any audio — recording empty turn"
                    )
        except asyncio.CancelledError as e:
            # Save the original exception so we can re-raise it after
            # the post-stream bookkeeping runs. Bare `raise` at the
            # end of the function would raise
            # RuntimeError("No active exception to re-raise") because
            # sys.exc_info() is cleared when the except block exits.
            cancelled_during_stream = True
            cancel_exc = e
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except (asyncio.CancelledError, Exception):
                    pass
            # DO NOT re-raise yet — we need the post-stream bookkeeping
            # (mark_tts_complete, _persist_turn) to run. Re-raise at
            # the end of the function so the parent task still sees
            # the cancel.
        finally:
            # MUST run on every exit path (success, cancel, post-stream
            # exception, bare raise). Without this, the next STT final
            # after a successful TTS turn hits
            # `if self.client_is_speaking: return` in _on_stt_result
            # and is silently dropped ("Skipping STT (TTS still
            # playing)") — the agent becomes unresponsive until an
            # abort or inject flows through. Re-introduced after the
            # try/except restructuring removed it.
            self.client_is_speaking = False
        # ── Post-stream tail ──────────────────────────────────────────
        # Everything from producer-drain through persist + cancel
        # re-raise is wrapped in a single try/except so an exception
        # anywhere in the tail (closed WS, OOM in wave.write, OSErr on
        # the debug save, etc.) still produces a coherent TurnStorage
        # record and still surfaces to the parent task.
        post_stream_exc: Exception | None = None
        full_text = ""
        try:
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

            # Post-stream bookkeeping. If we were cancelled mid-stream,
            # use ``truncated_reason="cancelled"`` (or "barge_in" if the
            # interrupt event was set) instead of the normal "complete" /
            # "drain_timeout" outcomes. We still re-raise the cancel at
            # the end of this block so the parent task sees it.
            if tts_started:
                tts_ok = False
                tts_truncated_reason = ""
                if cancelled_during_stream:
                    # Cancel arrived during streaming — TTS was interrupted.
                    tts_truncated_reason = (
                        "barge_in" if self._interrupt_event.is_set()
                        else "cancelled"
                    )
                elif stream_failed_mid_turn:
                    # The WS stream raised an exception after some
                    # TTS had already been sent to the device. The
                    # device received a prefix of the audio but no
                    # clean end — don't claim "complete".
                    # (post_stream_exc is read separately below, in
                    # the except Exception handler, where it gates a
                    # second mark_tts_complete with stream_failed.)
                    tts_truncated_reason = "stream_failed"
                elif await self._wait_playback_drain() and not self._interrupt_event.is_set():
                    await self._ws.send(json.dumps({
                        "session_id": self.session_id,
                        "type": "tts",
                        "state": "stop",
                    }))
                    tts_ok = True
                    TelemetryCollector.mark("tts_end")
                else:
                    tts_truncated_reason = "drain_timeout"
                # H5: 不在这里 finish_turn。子 task 调 finish_turn 只能 mark
                # _finished,真正 flush (_log + 清 ctx) 由父 task 在 await
                # tts_task 之后做。这里只 mark _finished。
                TelemetryCollector.finish_turn()
                # M1: TTS 结束,清掉当前 sentence_id 跟踪 (无论
                # tts_started 还是 no-tts-started 都清,避免上一个
                # turn 的 sentence_id 残留在下一个 turn 开头).
                self._current_tts_sentence_id = None
                # New path: record the TTS outcome on the TurnStorage so
                # commit() can fill tts_status / tts_truncated_reason in
                # the sidecar. This MUST run even on cancel so the sidecar
                # reflects what actually happened.
                if self._turn_storage is not None:
                    self._turn_storage.mark_tts_complete(
                        full_text,
                        ok=tts_ok,
                        truncated_reason=tts_truncated_reason,
                    )
                    # The except Exception handler below must NOT
                    # overwrite a clean tts_status with a misleading
                    # "stream_failed" — once bookkeeping has run, the
                    # first call wins.
                    bookkeeping_done = True

            # Persist assistant message. ensure_future is fire-and-forget;
            # wrap so a missing event loop / cancellation is logged rather
            # than silently dropping the assistant turn text.
            if full_text.strip() and self._pig and self._pig.ctx:
                self._schedule_persist_turn("assistant", full_text.strip())

            # If TTS never started (e.g. LLM returned empty, or the
            # request was interrupted before any audio was produced),
            # still record the turn so the input.wav + empty tts.wav
            # pair is preserved in S3.
            if not tts_started and self._turn_storage is not None:
                truncated_reason = "no_tts_started"
                if self._interrupt_event.is_set():
                    truncated_reason = "barge_in"
                elif cancelled_during_stream:
                    truncated_reason = "cancelled"
                self._turn_storage.mark_tts_complete(
                    full_text,
                    ok=False,
                    truncated_reason=truncated_reason,
                )
                # The except Exception handler below must NOT
                # overwrite this with a misleading "stream_failed".
                bookkeeping_done = True
                # M1 (Round 4 fix): also clear _current_tts_sentence_id
                # in the no-tts-started path. Without this, an LLM-
                # empty reply leaves the previous turn's sentence_id
                # lingering into the next turn, mis-attributing any
                # late tts_played playback timing to the new turn.
                self._current_tts_sentence_id = None
        except asyncio.CancelledError:
            # Cancel during the post-stream tail. Propagate so the
            # parent task sees it. (The outer `try`'s except
            # CancelledError did NOT re-raise — it saved the
            # instance in `cancel_exc` for re-raise after the tail.)
            raise
        except Exception as e:
            # Any other exception in the tail (closed WS, OOM, OSError
            # on the debug WAV, etc.). Record it, mark the TurnStorage
            # with a stream_failed reason, and swallow. The parent
            # task's `except (CancelledError, Exception): pass` already
            # hides this from the WS loop; the goal is just to keep
            # bookkeeping coherent.
            post_stream_exc = e
            logger.exception(
                f"[Voice] Post-stream tail raised {type(e).__name__}: {e}"
            )
            if (
                self._turn_storage is not None
                and tts_started
                and not bookkeeping_done
            ):
                # The `if tts_started:` block above didn't run (or
                # exited before mark_tts_complete was called). Force
                # a stream_failed outcome. If bookkeeping_done is
                # already True, the first call set tts_status to
                # whatever the real outcome was (complete /
                # drain_timeout / barge_in / cancelled) — don't
                # overwrite that with a misleading "stream_failed".
                self._turn_storage.mark_tts_complete(
                    full_text,
                    ok=False,
                    truncated_reason="stream_failed",
                )

        # Re-raise the cancel (if any) so the parent task sees it.
        # This is OUTSIDE the tail's try/except: if the tail raised
        # a CancelledError, the except block re-raised and the
        # function exits with that exception — we don't get here
        # and we don't double-raise. If the tail raised an Exception
        # we caught and swallowed, we get here and propagate the
        # streaming cancel cleanly. If the tail completed normally,
        # we get here and propagate any streaming cancel that
        # happened before the tail ran.
        if cancel_exc is not None:
            raise cancel_exc
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

    def _schedule_persist_turn(self, role: str, content: str) -> None:
        """Fire-and-forget wrapper around _persist_turn.

        asyncio.ensure_future raises RuntimeError synchronously if
        called when no event loop is running (e.g. during shutdown
        / after the connection has been closed). We don't want a
        stale WS teardown to swallow the assistant turn text into a
        NoEventLoop error, so catch and log.
        """
        try:
            asyncio.ensure_future(self._persist_turn(role, content))
        except RuntimeError:
            logger.warning(
                f"[Voice] Skipping persist role={role} — no running event loop"
            )

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
                # If a real turn is in flight, its TurnStorage owns
                # tts_pcm_buf and we must not append inject audio
                # to it — that would leak the inject's voice into
                # the next turn's S3 commit. Save the inject's TTS
                # WAV to a separate diagnostic file instead.
                # (The inject itself is a "side effect" of the
                # underlying turn, but the audio is the inject's
                # responsibility; the real turn already wrote its
                # own TTS.)
                if len(tts_pcm) > 0:
                    if self._turn_storage is not None:
                        self._save_inject_tts_wav(bytes(tts_pcm), sentence_id)
                    else:
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
