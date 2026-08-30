"""End-to-end loopback test: a scripted device client drives a PiguguSession.

Validates the M1 wire path locally without real hardware: hello handshake,
Opus decode/encode round-trip through the pipeline, and the tts start/stop
protocol framing driven by listen states.
"""

import asyncio
import json

import numpy as np
import opuslib
import pytest
import websockets
from websockets.asyncio.server import serve

from voice.pipecat.session import PiguguSession
from voice.pipecat.pigugu_serializer import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE


def _make_opus_frames(n: int = 3) -> list[bytes]:
    encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, "voip")
    frames = []
    for _ in range(n):
        samples = (np.sin(np.arange(FRAME_SAMPLES) / FRAME_SAMPLES * 2 * np.pi * 440) * 8000).astype(
            np.int16
        )
        frames.append(encoder.encode(samples.tobytes(), FRAME_SAMPLES))
    return frames


async def _run_loopback_test() -> list[str]:
    events: list[str] = []

    async def on_connect(websocket):
        session = PiguguSession(websocket, client_id="test-client", session_id="t1")
        await session.run()

    async def client(port: int):
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "version": 1,
                        "features": {"mcp": True},
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
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello["type"] == "hello", hello
            assert hello["audio_params"]["format"] == "opus"
            events.append("hello_reply")

            await ws.send(json.dumps({"session_id": "t1", "type": "listen", "state": "start"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["type"] == "tts" and msg["state"] == "start", msg
            events.append("tts_start")

            for i, opus in enumerate(_make_opus_frames()):
                await ws.send(opus)
                echo = await asyncio.wait_for(ws.recv(), timeout=5)
                assert isinstance(echo, bytes), echo
                # Echo must decode to a full 60ms PCM frame.
                pcm = opuslib.Decoder(SAMPLE_RATE, CHANNELS).decode(echo, FRAME_SAMPLES)
                assert len(pcm) == FRAME_SAMPLES * CHANNELS * 2
                events.append(f"echo_{i}")

            await ws.send(
                json.dumps(
                    {"session_id": "t1", "type": "listen", "state": "vad_silence", "user_stop_age_ms": 0}
                )
            )
            stop = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert stop["type"] == "tts" and stop["state"] == "stop", stop
            events.append("tts_stop")

    async with serve(on_connect, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(client(port), timeout=15)

    return events


@pytest.mark.asyncio
async def test_pigugu_loopback_full_wire_path():
    events = await _run_loopback_test()
    assert events == ["hello_reply", "tts_start", "echo_0", "echo_1", "echo_2", "tts_stop"]
