"""Silero VAD provider — double-threshold + sliding window + silence tracking.

Adapted from the official xiaozhi-esp32-server implementation:
  core/providers/vad/silero.py (xinnan-tech/xiaozhi-esp32-server)

Uses ``silero-vad-lite`` (ONNX-based, no PyTorch) for inference.
Each connection gets its own sliding window and state via the ``conn`` object.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
from loguru import logger

from providers.base import VADProvider

# ── Lazy-loaded model (shared across all connections) ────────────────
_model: Any = None


def _get_model() -> Any:
    global _model
    if _model is None:
        from silero_vad_lite import SileroVAD  # pyright: ignore[reportMissingImports]

        _model = SileroVAD(16000)
    return _model


class SileroVAD(VADProvider):
    """Double-threshold Silero VAD with sliding window confirmation.

    Parameters
    ----------
    threshold : float
        High confidence threshold for speech start (default 0.5).
    threshold_low : float
        Low confidence threshold to maintain speech state (default 0.2).
    min_silence_duration_ms : int
        Sustained silence required to confirm voice stop (default 1000 ms).
    frame_window_size : int
        Sliding window size for confirmation (default 5).
    frame_window_threshold : int
        Minimum number of speech frames in window (default 3).
    """

    def __init__(
        self,
        threshold: float = 0.5,
        threshold_low: float = 0.2,
        min_silence_duration_ms: int = 700,
        frame_window_size: int = 5,
        frame_window_threshold: int = 3,
    ):
        self.model = _get_model()
        self.vad_threshold = threshold
        self.vad_threshold_low = threshold_low
        self.silence_threshold_ms = min_silence_duration_ms
        self.frame_window_size = frame_window_size
        self.frame_window_threshold = frame_window_threshold

    # ── Per-connection state init / teardown ──────────────────────────

    def release_conn_resources(self, conn: Any) -> None:
        for attr in ("_vad_pcm_buffer", "_voice_window", "last_is_voice"):
            if hasattr(conn, attr):
                try:
                    delattr(conn, attr)
                except Exception:
                    pass

    # ── Core VAD logic ────────────────────────────────────────────────

    def is_vad(self, conn: Any, pcm_frame: bytes) -> bool:
        """Returns True if the frame (or recent window) contains speech.

        Stores per-connection VAD state on ``conn``:
        - ``conn._vad_pcm_buffer`` : accumulated PCM until >= 512 samples
        - ``conn._voice_window`` : deque of recent voice booleans
        - ``conn.last_is_voice`` : previous per-chunk voice flag
        - ``conn.client_have_voice`` : True once sliding window confirms speech
        - ``conn.client_voice_stop`` : True when sustained silence triggers end
        - ``conn.vad_last_voice_time`` : timestamp (ms) of last confirmed speech
        """
        # Manual mode: always return True (all audio is kept)
        if getattr(conn, "client_listen_mode", "auto") == "manual":
            return True

        try:
            # -- Init per-connection state on first call --
            if not hasattr(conn, "_vad_pcm_buffer"):
                conn._vad_pcm_buffer = bytearray()
            if not hasattr(conn, "_voice_window"):
                from collections import deque

                conn._voice_window = deque(maxlen=self.frame_window_size)
            if not hasattr(conn, "last_is_voice"):
                conn.last_is_voice = False

            # -- Accumulate PCM; process in ~32ms chunks (512 samples) --
            conn._vad_pcm_buffer.extend(pcm_frame)

            client_have_voice = False

            while len(conn._vad_pcm_buffer) >= 512 * 2:
                chunk = conn._vad_pcm_buffer[: 512 * 2]
                conn._vad_pcm_buffer = conn._vad_pcm_buffer[512 * 2 :]

                # silero-vad-lite expects float32 [-1, 1]
                audio_int16 = np.frombuffer(bytes(chunk), dtype=np.int16)
                audio_float32 = audio_int16.astype(np.float32) / 32768.0
                speech_prob = self.model.process(audio_float32)

                # -- Double-threshold hysteresis --
                if speech_prob >= self.vad_threshold:
                    is_voice = True
                elif speech_prob <= self.vad_threshold_low:
                    is_voice = False
                else:
                    is_voice = conn.last_is_voice

                conn.last_is_voice = is_voice

                # -- Sliding window confirmation --
                conn._voice_window.append(is_voice)
                client_have_voice = (
                    conn._voice_window.count(True) >= self.frame_window_threshold
                )

                # -- Voice start / stop transitions --
                prev_have_voice = getattr(conn, "client_have_voice", False)

                if not prev_have_voice and client_have_voice:
                    # Speech just started
                    logger.debug(
                        f"[VAD] Speech start prob={speech_prob:.2f} "
                        f"session={getattr(conn, 'session_id', '?')}"
                    )

                if prev_have_voice and not client_have_voice:
                    # Speech ended — check silence duration
                    now_ms = time.time() * 1000
                    last_voice = getattr(conn, "vad_last_voice_time", 0.0)
                    stop_duration = now_ms - last_voice
                    if stop_duration >= self.silence_threshold_ms:
                        conn.client_voice_stop = True
                        conn.client_have_voice = False  # reset for next voice cycle
                        logger.info(
                            f"[VAD] Voice stop: silence={stop_duration:.0f}ms "
                            f"session={getattr(conn, 'session_id', '?')}"
                        )

                if client_have_voice:
                    conn.client_have_voice = True
                    conn.vad_last_voice_time = time.time() * 1000

            return client_have_voice

        except Exception:
            logger.exception("[VAD] Error processing audio packet")
            return True  # assume speech on error — no false-timeout
