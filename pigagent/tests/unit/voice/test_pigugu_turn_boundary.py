"""M2 test: turn boundary is voice-signal driven, not per-STT-final.

Simulates the 6be1be41 scenario: Deepgram splits one continuous utterance into
several ``is_final`` chunks. The fake STT emits three finals inside a single
user turn (bounded by device ``listen/start`` + ``listen/vad_silence``); the
aggregator must merge them into ONE turn.
"""

import asyncio
import json

import numpy as np
import opuslib
import pytest
import websockets
from websockets.asyncio.server import serve
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from providers.vad.silero import SileroVAD
from voice.pipecat.agent_gateway import PiguguAgentGateway
from voice.pipecat.pigugu_serializer import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE
from voice.pipecat.session import PiguguSession
from voice.pipecat.stt_bridge import PiguguSttBridge
from voice.pipecat.vad_bridge import PiguguVadBridge

SPLIT_FINALS = ["alexa good", "evening", "how are you"]
EXPECTED_MERGED = "alexa good evening how are you"


class FakeSTT:
    """Emits the split finals on the first audio frame, then stays quiet."""

    interface_type = "stream"

    def __init__(self):
        self._emitted = False

    async def open_audio_channels(self, conn):
        pass

    async def receive_audio(self, conn, pcm, have_voice):
        if not self._emitted and pcm:
            self._emitted = True
            for text in SPLIT_FINALS:
                await conn._on_stt_final(text)


def _make_opus_tone() -> bytes:
    enc = opuslib.Encoder(SAMPLE_RATE, CHANNELS, "voip")
    samples = (
        np.sin(np.arange(FRAME_SAMPLES) / FRAME_SAMPLES * 2 * np.pi * 440) * 8000
    ).astype(np.int16)
    return enc.encode(samples.tobytes(), FRAME_SAMPLES)


async def _run(port: int, turn_q: asyncio.Queue) -> str:
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
        )
        await asyncio.wait_for(ws.recv(), 5)  # hello reply
        await ws.send(json.dumps({"type": "listen", "state": "start"}))
        # A few audio frames so the fake STT fires its finals.
        for _ in range(3):
            await ws.send(_make_opus_tone())
        await asyncio.sleep(0.2)
        await ws.send(
            json.dumps({"type": "listen", "state": "vad_silence", "user_stop_age_ms": 0})
        )
        # The turn merges server-side after the 0.6s speech timeout — wait for
        # it before closing, or the disconnect stops the worker prematurely.
        merged = await asyncio.wait_for(turn_q.get(), timeout=5)
        await ws.close()
        return merged


@pytest.mark.asyncio
async def test_split_finals_merge_into_one_turn():
    turn_q: asyncio.Queue = asyncio.Queue()

    async def record_turn(t: str):
        turn_q.put_nowait(t)

    def make_session(ws):
        aggregator = PiguguAgentGateway(on_turn=record_turn)
        chain = [
            PiguguVadBridge(SileroVAD()),
            PiguguSttBridge(FakeSTT()),
            UserTurnProcessor(
                user_turn_strategies=UserTurnStrategies(
                    start=[VADUserTurnStartStrategy()],
                    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
                ),
            ),
            aggregator,
        ]
        return PiguguSession(ws, client_id="m2", session_id="m2s", processors=chain)

    async def on_connect(ws):
        session = make_session(ws)
        await session.run()

    async with serve(on_connect, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        merged = await asyncio.wait_for(_run(port, turn_q), timeout=10)
        assert merged == EXPECTED_MERGED
