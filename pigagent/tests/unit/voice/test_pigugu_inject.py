"""M4 test: external inject interrupts the current reply and plays its text.

Runs a full turn, then calls ``session.inject()`` (the REST roast path) while
the assistant is speaking. The device must see, in order: tts/abort (flushing
the interrupted reply), then tts/start + audio + tts/stop for the injected
text — and no extra turn is built (the interrupted reply still commits its
own storage separately).
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


class FakeAgent:
    interface_type = "stream"

    async def generate_reply(
        self, user_text, *, persona_id=1, interrupt_event=None, session_id=None
    ):
        # Long reply so the inject lands mid-playback.
        for _ in range(4):
            if interrupt_event and interrupt_event.is_set():
                return
            yield "lots of words "


class FakeTTS:
    interface_type = "stream"

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        async for text in text_source:
            if interrupt_event and interrupt_event.is_set():
                break
            yield [_make_opus_tone() for _ in range(30)]  # 1.8s per batch

    async def synthesize(self, text, collect_pcm=None):
        if collect_pcm is not None:
            collect_pcm.extend(b"\x00" * (FRAME_SAMPLES * 2 * 6))
        return [_make_opus_tone() for _ in range(6)]  # 0.36s inject


class FakeSTT:
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
            await conn._on_utterance_end()


def _make_opus_tone() -> bytes:
    enc = opuslib.Encoder(SAMPLE_RATE, CHANNELS, "voip")
    samples = (
        np.sin(np.arange(FRAME_SAMPLES) / FRAME_SAMPLES * 2 * np.pi * 440) * 8000
    ).astype(np.int16)
    return enc.encode(samples.tobytes(), FRAME_SAMPLES)


async def _run(port: int, session_q: asyncio.Queue) -> list[tuple[str, object]]:
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
        # Wait for the reply to start, then fire the inject mid-playback.
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, str):
                parsed = json.loads(msg)
                seen.append(("msg", parsed))
                if parsed.get("type") == "tts" and parsed.get("state") == "start":
                    break
            else:
                seen.append(("audio", msg))
        session = await asyncio.wait_for(session_q.get(), timeout=5)
        await session.inject({"type": "roast", "text": "great job!"})
        # Read until the inject's tts/stop.
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
        return seen


@pytest.mark.asyncio
async def test_inject_interrupts_reply():
    session_q: asyncio.Queue = asyncio.Queue()

    def make_session(ws):
        session = PiguguSession(
            ws,
            client_id="m4i",
            session_id="m4i",
            vad=None,
            stt=FakeSTT(),
            pig=FakeAgent(),
            tts=FakeTTS(),
        )
        session_q.put_nowait(session)
        return session

    async def on_connect(ws):
        await make_session(ws).run()

    async with serve(on_connect, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        seen = await asyncio.wait_for(_run(port, session_q), timeout=15)

    states = [p["state"] for kind, p in seen if kind == "msg" and p["type"] == "tts"]
    # The interrupted reply is flushed, then the inject plays its own turn.
    assert states[0] == "start"      # reply started
    assert states[1] == "abort"      # inject flushed the reply
    assert states[2] == "start"      # inject playback
    assert states[3] == "stop"       # inject drained
    audio = [p for kind, p in seen if kind == "audio"]
    assert len(audio) >= 6
    # The interrupted reply must NOT emit a trailing tts/stop.
    assert states.count("stop") == 1
