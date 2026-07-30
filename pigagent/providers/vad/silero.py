"""Silero VAD provider — ONNX Runtime + double-threshold + sliding window.

Mirrors the official xiaozhi-esp32-server implementation:
  core/providers/vad/silero.py (xinnan-tech/xiaozhi-esp32-server)

Uses ONNX Runtime directly (same as official), no PyTorch.
Model file: providers/vad/model/silero_vad.onnx
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from providers.base import VADProvider

_MODEL_PATH = Path(__file__).resolve().parent / "model" / "silero_vad.onnx"


class SileroVAD(VADProvider):
    """Double-threshold Silero VAD with sliding window confirmation.

    Uses ONNX Runtime for inference — identical to the official
    xiaozhi-esp32-server implementation.
    """

    def __init__(
        self,
        threshold: float = 0.5,
        threshold_low: float = 0.2,
        min_silence_duration_ms: int = 700,
        frame_window_threshold: int = 3,
    ):
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            str(_MODEL_PATH), providers=["CPUExecutionProvider"], sess_options=opts
        )

        self.vad_threshold = threshold
        self.vad_threshold_low = threshold_low
        self.silence_threshold_ms = min_silence_duration_ms
        self.frame_window_threshold = frame_window_threshold
        logger.info(f"[VAD] Silero ONNX loaded threshold={threshold}")

    def _init_connection_state(self, conn: Any) -> None:
        """Initialise per-connection VAD state arrays."""
        if not hasattr(conn, "_vad_state"):
            conn._vad_state = np.zeros((2, 1, 128), dtype=np.float32)
        if not hasattr(conn, "_vad_context"):
            conn._vad_context = np.zeros((1, 64), dtype=np.float32)

    def release_conn_resources(self, conn: Any) -> None:
        for attr in ("_vad_state", "_vad_context"):
            if hasattr(conn, attr):
                try:
                    delattr(conn, attr)
                except Exception:
                    pass

    def is_vad(self, conn: Any, pcm_frame: bytes) -> bool:
        # Manual mode: always return True (all audio is kept)
        if getattr(conn, "client_listen_mode", "auto") == "manual":
            return True

        try:
            self._init_connection_state(conn)

            conn.client_audio_buffer.extend(pcm_frame)

            client_have_voice = False
            while len(conn.client_audio_buffer) >= 512 * 2:
                chunk = conn.client_audio_buffer[: 512 * 2]
                conn.client_audio_buffer = conn.client_audio_buffer[512 * 2 :]

                audio_int16 = np.frombuffer(chunk, dtype=np.int16)
                audio_float32 = audio_int16.astype(np.float32) / 32768.0
                audio_input = np.concatenate(
                    [conn._vad_context, audio_float32.reshape(1, -1)], axis=1
                ).astype(np.float32)

                ort_inputs = {
                    "input": audio_input,
                    "state": conn._vad_state,
                    "sr": np.array(16000, dtype=np.int64),
                }
                out, state = self.session.run(None, ort_inputs)

                conn._vad_state = state
                conn._vad_context = audio_input[:, -64:]
                speech_prob = out.item()

                # Double-threshold hysteresis
                if speech_prob >= self.vad_threshold:
                    is_voice = True
                elif speech_prob <= self.vad_threshold_low:
                    is_voice = False
                else:
                    is_voice = getattr(conn, "last_is_voice", False)

                conn.last_is_voice = is_voice

                # Sliding window confirmation
                conn.client_voice_window.append(is_voice)
                client_have_voice = (
                    conn.client_voice_window.count(True)
                    >= self.frame_window_threshold
                )

                # Voice → silence transition with sustained silence check
                if conn.client_have_voice and not client_have_voice:
                    now_ms = time.time() * 1000
                    last = getattr(conn, "vad_last_voice_time", 0.0)
                    stop_duration = now_ms - last
                    if stop_duration >= self.silence_threshold_ms:
                        conn.client_voice_stop = True
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
