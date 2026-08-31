"""M3 test: full conversation loop — STT → turn → LLM → Cartesia → paced Opus.

The fake STT splits one utterance into several finals; the gateway merges them
into ONE turn; the TTS bridge runs the fake agent + fake TTS and paces Opus
frames out; the device receives, in protocol order::

    tts/start (sentence_id=1) → opus frames → tts/stop

and the agent is invoked with the merged utterance, not per-final fragments.
"""

import asyncio
import json

import numpy as np
import opuslib
import pytest
import websockets
from websockets.asyncio.server import serve

from voice.pipecat.pigugu_serializer import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE
from voice.pipecat.session import PiguguSession

SPLIT_FINALS = ["alexa good", "evening", "how are you"]
EXPECTED_MERGED = "alexa good evening how are you"
REPLY_CHUNKS = ["hi there, ", "this is pigugu!"]
FRAMES_PER_BATCH = 6


class FakeAgent:
    interface_type = "stream"

    def __init__(self, captured: list[str]):
        self._captured = captured

    async def generate_reply(
        self, user_text, *, persona_id=1, interrupt_event=None, session_id=None
    ):
        self._captured.append(user_text)
        for chunk in REPLY_CHUNKS:
            if interrupt_event and interrupt_event.is_set():
                return
            yield chunk


class FakeTTS:
    interface_type = "stream"

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None):
        async for text in text_source:
            if interrupt_event and interrupt_event.is_set():
                break
            yield [_make_opus_tone() for _ in range(FRAMES_PER_BATCH)]

    async def synthesize(self, text, collect_pcm=None):
        return [_make_opus_tone() for _ in range(FRAMES_PER_BATCH)]


class FakeSTT:
    interface_type = "stream"

    def __init__(self, interim_after_start: bool = False):
        self._emitted = False
        # Barge-in mode: fire an interim (user talks over the assistant) once
        # the caller signals the assistant has started speaking.
        self.fire_interim = False
        self._interim_after_start = interim_after_start

    async def open_audio_channels(self, conn):
        pass

    async def receive_audio(self, conn, pcm, have_voice):
        if not pcm:
            return
        if not self._emitted:
            self._emitted = True
            # Finals arrive spaced (like Deepgram streaming), not in one batch:
            # the first final starts the turn and broadcasts an interruption
            # (pipeline reset), so later finals must arrive after that reset to
            # accumulate into the same turn.
            for text in SPLIT_FINALS:
                await conn._on_stt_final(text)
                await asyncio.sleep(0.3)
            await conn._on_utterance_end()
        if self._interim_after_start and self.fire_interim:
            self.fire_interim = False
            # 3 words: MinWords requires min_words=3 while the bot is speaking.
            await conn._on_stt_interim("stop it now")


def _make_opus_tone() -> bytes:
    enc = opuslib.Encoder(SAMPLE_RATE, CHANNELS, "voip")
    samples = (
        np.sin(np.arange(FRAME_SAMPLES) / FRAME_SAMPLES * 2 * np.pi * 440) * 8000
    ).astype(np.int16)
    return enc.encode(samples.tobytes(), FRAME_SAMPLES)


async def _run(port: int) -> dict:
    seen: list[tuple[str, object]] = []
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
        hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        await ws.send(json.dumps({"type": "listen", "state": "start"}))
        # Audio has system priority in pipecat's queue — let the START reach
        # the turn processor before the fake STT's utterance-end fires.
        await asyncio.sleep(0.2)
        for _ in range(3):
            await ws.send(_make_opus_tone())
        # Read until the turn's tts/stop — pacing holds it open ~0.7s.
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, str):
                parsed = json.loads(msg)
                seen.append(("msg", parsed))
                if parsed.get("type") == "tts" and parsed.get("state") == "stop":
                    break
            else:
                seen.append(("audio", msg))
        await ws.close()
        return {"hello": hello, "seen": seen}


@pytest.mark.asyncio
async def test_full_conversation_loop():
    captured: list[str] = []
    agent = FakeAgent(captured)

    def make_session(ws):
        return PiguguSession(
            ws,
            client_id="m3",
            session_id="m3s",
            vad=None,
            stt=FakeSTT(),
            pig=agent,
            tts=FakeTTS(),
        )

    async def on_connect(ws):
        session = make_session(ws)
        await session.run()

    async with serve(on_connect, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        result = await asyncio.wait_for(_run(port), timeout=10)

    seen = result["seen"]
    # Protocol order: the merged stt text first (device displays it), then
    # tts/start, audio, and finally tts/stop.
    assert seen[0][0] == "msg"
    stt = seen[0][1]
    assert stt["type"] == "stt" and stt["text"] == EXPECTED_MERGED
    start = next(p for kind, p in seen if kind == "msg" and p.get("type") == "tts" and p.get("state") == "start")
    assert start["sentence_id"] == 1
    assert seen[-1][0] == "msg"
    stop = seen[-1][1]
    assert stop["type"] == "tts" and stop["state"] == "stop"

    # Some Opus audio was paced out and decodes cleanly.
    audio = [p for kind, p in seen if kind == "audio"]
    assert len(audio) >= FRAMES_PER_BATCH
    dec = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
    for frame in audio:
        assert dec.decode(frame, FRAME_SAMPLES)

    # The agent got ONE merged utterance, not the three finals.
    assert captured == [EXPECTED_MERGED]


async def _run_barge_in(port: int, stt: FakeSTT) -> list[tuple[str, object]]:
    """Play a full turn, then talk over the assistant; collect device messages."""
    seen: list[tuple[str, object]] = []
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
        # Audio has system priority in pipecat's queue — let the START reach
        # the turn processor before the fake STT's utterance-end fires.
        await asyncio.sleep(0.2)
        for _ in range(3):
            await ws.send(_make_opus_tone())
        # Wait for tts/start so we know the assistant is speaking.
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, str):
                parsed = json.loads(msg)
                seen.append(("msg", parsed))
                if parsed.get("type") == "tts" and parsed.get("state") == "start":
                    break
            else:
                seen.append(("audio", msg))
        # User talks over the assistant → interim STT → barge-in.
        stt.fire_interim = True
        await ws.send(_make_opus_tone())
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, str):
                parsed = json.loads(msg)
                seen.append(("msg", parsed))
                if parsed.get("type") == "tts" and parsed.get("state") == "abort":
                    break
            else:
                seen.append(("audio", msg))
        await ws.close()
        return seen


@pytest.mark.asyncio
async def test_barge_in_aborts_playback():
    stt = FakeSTT(interim_after_start=True)

    def make_session(ws):
        return PiguguSession(
            ws,
            client_id="m3b",
            session_id="m3b",
            vad=None,
            stt=stt,
            pig=FakeAgent([]),
            tts=FakeTTS(),
        )

    async def on_connect(ws):
        session = make_session(ws)
        await session.run()

    async with serve(on_connect, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        seen = await asyncio.wait_for(_run_barge_in(port, stt), timeout=10)

    states = [p["state"] for kind, p in seen if kind == "msg" and p["type"] == "tts"]
    # tts/start → (audio) → tts/abort; an interrupted turn sends NO tts/stop.
    assert states[0] == "start"
    assert states[-1] == "abort"
    assert "stop" not in states


async def _run_device_abort(port: int) -> list[tuple[str, object]]:
    """Play a turn, then the device sends an explicit abort (stop button)."""
    seen: list[tuple[str, object]] = []
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
        await asyncio.wait_for(ws.recv(), 5)
        await ws.send(json.dumps({"type": "listen", "state": "start"}))
        # Audio has system priority in pipecat's queue — let the START reach
        # the turn processor before the fake STT's utterance-end fires.
        await asyncio.sleep(0.2)
        for _ in range(3):
            await ws.send(_make_opus_tone())
        # Wait for playback to begin, then abort.
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, str):
                parsed = json.loads(msg)
                seen.append(("msg", parsed))
                if parsed.get("type") == "tts" and parsed.get("state") == "start":
                    break
            else:
                seen.append(("audio", msg))
        await ws.send(json.dumps({"type": "abort", "reason": "button"}))
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, str):
                parsed = json.loads(msg)
                seen.append(("msg", parsed))
                if parsed.get("type") == "tts" and parsed.get("state") == "abort":
                    break
            else:
                seen.append(("audio", msg))
        await ws.close()
        return seen


@pytest.mark.asyncio
async def test_device_abort_flushes_playback():
    def make_session(ws):
        return PiguguSession(
            ws,
            client_id="m3a",
            session_id="m3a",
            vad=None,
            stt=FakeSTT(),
            pig=FakeAgent([]),
            tts=FakeTTS(),
        )

    async def on_connect(ws):
        session = make_session(ws)
        await session.run()

    async with serve(on_connect, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        seen = await asyncio.wait_for(_run_device_abort(port), timeout=10)

    states = [p["state"] for kind, p in seen if kind == "msg" and p["type"] == "tts"]
    # start → audio → abort (device flush); NO trailing tts/stop from the
    # interrupted reply.
    assert states[0] == "start"
    assert states[-1] == "abort"
    assert "stop" not in states
