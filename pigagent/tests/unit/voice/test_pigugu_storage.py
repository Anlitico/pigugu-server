"""M4 test: per-turn TurnStorage is built, filled, and committed end-to-end.

Runs the full loop (like the M3 conversation test) with S3/ClickHouse env vars
set and TurnStorage's I/O stubbed so commit() completes in memory. Asserts:
- exactly one turn committed, with the merged STT text + reply TTS text,
- user PCM captured over the turn window, tts PCM collected from the fake TTS,
- tts_status == complete,
- device_playback_ms from the tts_played ack lands in telemetry,
- s3_uris populated (the 6-file layout).
"""

import asyncio
import json
import time
import uuid

import numpy as np
import opuslib
import pytest
import websockets
from websockets.asyncio.server import serve

from voice.interims import InterimBuffer
from voice.pipecat.pigugu_serializer import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE
from voice.pipecat.session import PiguguSession
from voice.pipecat.state import PiguguTurnState
from voice.pipecat.turn_storage_observer import PiguguTurnStorageObserver
from voice.storage import TurnStorage

SPLIT_FINALS = ["alexa good", "evening", "how are you"]
EXPECTED_MERGED = "alexa good evening how are you"
REPLY_CHUNKS = ["hi there, ", "this is pigugu!"]
FRAMES_PER_BATCH = 6


class FakeAgent:
    interface_type = "stream"

    async def generate_reply(
        self, user_text, *, persona_id=1, interrupt_event=None, session_id=None
    ):
        for chunk in REPLY_CHUNKS:
            if interrupt_event and interrupt_event.is_set():
                return
            yield chunk


class FakeTTS:
    interface_type = "stream"

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        async for text in text_source:
            if interrupt_event and interrupt_event.is_set():
                break
            frames = [_make_opus_tone() for _ in range(FRAMES_PER_BATCH)]
            if collect_pcm is not None:
                collect_pcm.extend(b"\x00" * (FRAME_SAMPLES * 2 * FRAMES_PER_BATCH))
            yield frames

    async def synthesize(self, text, collect_pcm=None):
        frames = [_make_opus_tone() for _ in range(FRAMES_PER_BATCH)]
        if collect_pcm is not None:
            collect_pcm.extend(b"\x00" * (FRAME_SAMPLES * 2 * FRAMES_PER_BATCH))
        return frames


class FakeSTT:
    interface_type = "stream"

    def __init__(self):
        self._emitted = False

    async def open_audio_channels(self, conn):
        pass

    async def receive_audio(self, conn, pcm, have_voice):
        if not self._emitted and pcm:
            self._emitted = True
            # Finals arrive spaced (like Deepgram streaming), not in one batch:
            # the first final starts the turn (and broadcasts an interruption,
            # which resets the pipeline), so later finals must arrive AFTER that
            # reset to be accumulated into the same turn.
            for text in SPLIT_FINALS:
                await conn._on_stt_final(text)
                await asyncio.sleep(0.3)
            await conn._on_utterance_end()


def _make_opus_tone() -> bytes:
    enc = opuslib.Encoder(SAMPLE_RATE, CHANNELS, "voip")
    samples = (
        np.sin(np.arange(FRAME_SAMPLES) / FRAME_SAMPLES * 2 * np.pi * 440) * 8000
    ).astype(np.int16)
    return enc.encode(samples.tobytes(), FRAME_SAMPLES)


async def _run(port: int, *, send_tts_played: bool) -> None:
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
        # Wake word first — the following turn must be classified wake_word.
        await ws.send(json.dumps({"type": "listen", "state": "detect", "text": "pigugu"}))
        await ws.send(json.dumps({"type": "listen", "state": "start"}))
        # Audio has system priority in pipecat's queue — let the START reach
        # the turn processor before the fake STT's utterance-end fires.
        await asyncio.sleep(0.2)
        for _ in range(3):
            await ws.send(_make_opus_tone())
        # Wait for tts/start, ack playback, then read until tts/stop.
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, str):
                parsed = json.loads(msg)
                if parsed.get("type") == "tts" and parsed.get("state") == "start":
                    if send_tts_played:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "listen",
                                    "state": "tts_played",
                                    "device_playback_ms": 120,
                                    "sentence_id": parsed.get("sentence_id"),
                                }
                            )
                        )
                elif parsed.get("type") == "tts" and parsed.get("state") == "stop":
                    break
        await ws.close()


@pytest.mark.asyncio
async def test_turn_storage_commits_full_row(monkeypatch):
    monkeypatch.setenv("AUDIO_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("AUDIO_S3_PREFIX", "voice-turns")
    monkeypatch.setenv("CLICKHOUSE_HOST", "ch")
    monkeypatch.setenv("CLICKHOUSE_PORT", "9000")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "voice")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")

    committed: list[TurnStorage] = []

    async def fake_s3(self, payloads):
        for name in payloads:
            self.s3_uris[name] = f"s3://{self.s3_bucket}/{self.s3_prefix}/{name}"

    async def fake_ch_insert(self):
        committed.append(self)

    monkeypatch.setattr(TurnStorage, "_s3_upload_all", fake_s3)
    monkeypatch.setattr(TurnStorage, "_clickhouse_insert", fake_ch_insert)

    def make_session(ws):
        return PiguguSession(
            ws,
            client_id="m4",
            session_id="m4s",
            vad=None,
            stt=FakeSTT(),
            pig=FakeAgent(),
            tts=FakeTTS(),
        )

    async def on_connect(ws):
        session = make_session(ws)
        await session.run()

    async with serve(on_connect, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(_run(port, send_tts_played=True), timeout=10)

    # commit() is fire-and-forget — poll for it on the running loop.
    for _ in range(50):
        if committed:
            break
        await asyncio.sleep(0.05)
    assert len(committed) == 1
    storage = committed[0]

    assert storage.turn_idx == 1
    assert storage.session_id == "m4s"
    assert storage.device_id == "m4"
    # The wake-word turn is classified as such (listen/detect).
    assert storage.turn_type == "wake_word"
    # STT side: merged utterance, not the three finals.
    assert storage.stt_text == EXPECTED_MERGED
    assert storage.stt_status == "final"
    # TTS side: full reply text + collected PCM.
    assert storage.tts_text == "hi there, this is pigugu!"
    assert storage.tts_status == "complete"
    assert storage.tts_truncated_reason == ""
    # Audio windows.
    assert len(storage.user_pcm_bytes) > 0
    assert len(storage.tts_pcm_buf) > 0
    # The 6-file S3 layout (input / tts / listen + sidecars).
    assert set(storage.s3_uris) == {
        "input.wav", "input.json", "tts.wav", "tts.json", "listen.wav", "turn.json",
    }
    # device_playback_ms came through the tts_played ack.
    assert storage.telemetry.get("device_playback_ms") == 120
    # E2E segments present and REAL (marks recorded by the bridges land in a
    # shared turn dict — a regression here silently zeros every telemetry col).
    assert storage.telemetry.get("e2e_ms", 0) > 0
    assert storage.telemetry.get("llm_ttft_ms") is not None
    # voice_segments[] computed from the (empty, in this harness) VAD slice.
    assert isinstance(storage.voice_segments, list)


# ── M5d: listen.wav (post-turn listening / AEC probe) ────────────────


def _make_observer_turn(*, session_id: str, turn_idx: int) -> TurnStorage:
    return TurnStorage(
        turn_id=f"{int(time.time() * 1000)}_{session_id}_{turn_idx:04d}",
        session_id=session_id,
        turn_idx=turn_idx,
        device_id="d1",
        user_id="u1",
        persona_id=1,
        utc_start_ms=int(time.time() * 1000),
        s3_bucket="test-bucket",
        s3_prefix="voice-turns",
        clickhouse_dsn="clickhouse://default:secret@ch:9000/voice",
        clickhouse_table="voice.turns",
        interims=InterimBuffer(),
        voice_chunk_flags_slice=lambda: [],
    )


def _stub_turn_io(monkeypatch, committed: list) -> None:
    """Env + TurnStorage S3/CH stubs shared by the observer lifecycle tests."""
    monkeypatch.setenv("AUDIO_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("AUDIO_S3_PREFIX", "voice-turns")
    monkeypatch.setenv("CLICKHOUSE_HOST", "ch")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")

    async def fake_s3(self, payloads):
        for name in payloads:
            self.s3_uris[name] = f"s3://test-bucket/{self.s3_prefix}/{name}"

    async def fake_ch_insert(self):
        committed.append(self)

    monkeypatch.setattr(TurnStorage, "_s3_upload_all", fake_s3)
    monkeypatch.setattr(TurnStorage, "_clickhouse_insert", fake_ch_insert)


def _make_observer(monkeypatch, made: list) -> tuple:
    """Observer whose ``_make_storage`` records every storage it creates."""
    state = PiguguTurnState()
    observer = PiguguTurnStorageObserver(
        None, state, session_id="s1", client_id="d1", user_id="u1"
    )

    def fake_make_storage():
        s = _make_observer_turn(session_id="s1", turn_idx=len(made) + 1)
        made.append(s)
        return s

    monkeypatch.setattr(observer, "_make_storage", fake_make_storage)
    return state, observer


@pytest.mark.asyncio
async def test_observer_commits_at_finalize_with_reply_echo(monkeypatch):
    """Turn N commits as soon as the TTS bridge finalizes it (reply drained),
    WITHOUT waiting for the user's next utterance. Its listen.wav is the
    reply-period upstream mic audio (TTFT silence routed in at the reply-START
    edge + reply echo). A later turn or finalize_session is a no-op for it."""
    committed: list[TurnStorage] = []
    _stub_turn_io(monkeypatch, committed)
    made: list[TurnStorage] = []
    state, observer = _make_observer(monkeypatch, made)

    # Turn 1: assistant idle, user speaks → turn buffer. (The STT final
    # arrives before the stop, so this is a real turn, not a phantom.)
    state.client_is_speaking = False
    observer._route_audio(b"A" * 320)
    observer._route_audio(b"B" * 320)
    observer._saw_text = True
    await observer._on_user_stopped()
    storage1 = made[0]
    # Not finalized yet → nothing committed, no listen window.
    assert committed == []
    assert storage1.listen_pcm_bytes == b""

    # TTFT: reply not started yet, device still streams mic (latency silence).
    # This must land BEFORE the reply echo in turn 1's listen.
    ttft = b"S" * 160
    observer._route_audio(ttft)

    # The reply plays (assistant speaking): the reply-START edge moves the
    # TTFT silence into the gap buffer, then the echo accumulates after it.
    gap = b"G" * 640
    state.client_is_speaking = True
    observer._route_audio(gap)
    # Reply drained — the TTS bridge marks the storage finalized: the observer
    # commits THIS turn now, with no second utterance and no finalize_session.
    storage1.mark_finalized()
    await asyncio.sleep(0.05)  # let the commit-on-finalize waiter land
    assert committed == [storage1]
    assert storage1.listen_pcm_bytes == ttft + gap

    # Turn 2: reply done, user speaks → a fresh storage; turn 1 stays committed.
    state.client_is_speaking = False
    observer._route_audio(b"C" * 320)
    observer._saw_text = True
    await observer._on_user_stopped()
    await asyncio.sleep(0.05)
    storage2 = made[1]
    assert committed == [storage1]
    # Turn 2's listen must NOT inherit turn 1's echo; its input is its own.
    assert storage2.listen_pcm_bytes == b""
    assert storage2.user_pcm_bytes == b"C" * 320

    # Turn 2 commits at its own finalize too; finalize_session is then a no-op.
    storage2.mark_finalized()
    await asyncio.sleep(0.05)
    assert committed == [storage1, storage2]
    await observer.finalize_session()
    await asyncio.sleep(0.05)
    assert committed == [storage1, storage2]


@pytest.mark.asyncio
async def test_observer_commits_interrupted_reply_at_interruption(monkeypatch):
    """A reply cut off by barge-in commits AT the interruption (its storage is
    finalized by the TTS task's cancel finally) — not at the next user turn."""
    committed: list[TurnStorage] = []
    _stub_turn_io(monkeypatch, committed)
    made: list[TurnStorage] = []
    state, observer = _make_observer(monkeypatch, made)

    state.client_is_speaking = False
    observer._route_audio(b"A" * 320)
    observer._saw_text = True
    await observer._on_user_stopped()
    storage1 = made[0]
    assert committed == []

    # Reply plays → echo into the gap buffer.
    gap = b"G" * 640
    state.client_is_speaking = True
    observer._route_audio(gap)
    # Barge-in: the TTS task's finally records the interruption and finalizes.
    storage1.mark_tts_complete("partial", ok=False, truncated_reason="barge_in")
    storage1.mark_finalized()
    await asyncio.sleep(0.05)
    assert committed == [storage1]
    assert storage1.tts_status == "interrupted"
    assert storage1.tts_truncated_reason == "barge_in"
    assert storage1.listen_pcm_bytes == gap


@pytest.mark.asyncio
async def test_observer_finalize_session_backstops_never_finalized_storage(
    monkeypatch,
):
    """A turn whose reply never finalized (disconnect before the reply) is
    committed by ``finalize_session`` with the trailing audio + disconnect
    reason — the pure disconnect backstop."""
    monkeypatch.setattr(
        "voice.pipecat.turn_storage_observer._FINALIZE_TIMEOUT_SECS", 0.02
    )
    committed: list[TurnStorage] = []
    _stub_turn_io(monkeypatch, committed)
    made: list[TurnStorage] = []
    state, observer = _make_observer(monkeypatch, made)

    state.client_is_speaking = False
    observer._route_audio(b"A" * 320)
    observer._saw_text = True
    await observer._on_user_stopped()
    storage1 = made[0]
    assert committed == []

    # Trailing non-reply mic audio after the turn.
    tail = b"T" * 320
    observer._route_audio(tail)
    await observer.finalize_session()
    await asyncio.sleep(0.05)
    assert committed == [storage1]
    assert storage1.tts_truncated_reason == "disconnect"
    assert storage1.listen_pcm_bytes == tail


@pytest.mark.asyncio
async def test_observer_next_user_stop_sweeps_never_finalized_storage(
    monkeypatch,
):
    """The next user boundary stays a backstop for a storage that was never
    finalized (e.g. a wake-word transcript the gateway stripped to empty): it
    is committed there, not left dangling until disconnect."""
    monkeypatch.setattr(
        "voice.pipecat.turn_storage_observer._FINALIZE_TIMEOUT_SECS", 0.02
    )
    committed: list[TurnStorage] = []
    _stub_turn_io(monkeypatch, committed)
    made: list[TurnStorage] = []
    state, observer = _make_observer(monkeypatch, made)

    # Turn 1 stops but its reply never finalizes (no turn frame → no TTS).
    state.client_is_speaking = False
    observer._route_audio(b"A" * 320)
    observer._saw_text = True
    await observer._on_user_stopped()
    storage1 = made[0]
    assert committed == []

    # Turn 2's boundary sweeps turn 1.
    observer._route_audio(b"B" * 320)
    observer._saw_text = True
    await observer._on_user_stopped()
    await asyncio.sleep(0.05)
    assert len(made) == 2
    assert committed == [storage1]
    assert storage1.listen_pcm_bytes == b""  # no reply ever played


@pytest.mark.asyncio
async def test_observer_skips_no_transcript_turn(monkeypatch):
    """Regression: the wake-word audio burst drives a full VAD start→stop before
    Deepgram emits an is_final, so the first "turn" has audio but no transcript.
    The old code committed it as a phantom row (stt_status=no_stt,
    tts_status=empty) and abandoned the buffered interims — the user's first
    sentence got split and the device went silent on it. A no-transcript turn
    must create no storage and must carry its audio + interims into the
    following turn."""
    monkeypatch.setenv("AUDIO_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("AUDIO_S3_PREFIX", "voice-turns")
    monkeypatch.setenv("CLICKHOUSE_HOST", "ch")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")

    made: list[TurnStorage] = []

    def fake_make_storage():
        s = _make_observer_turn(session_id="s1", turn_idx=len(made) + 1)
        # Real _make_storage passes the shared interim buffer; the phantom turn
        # must leave it intact so this storage sees the preserved interims.
        s.interims = state.interims
        made.append(s)
        return s

    state = PiguguTurnState()
    observer = PiguguTurnStorageObserver(
        None, state, session_id="s1", client_id="d1", user_id="u1"
    )
    monkeypatch.setattr(observer, "_make_storage", fake_make_storage)

    # Wake-word burst: audio + Deepgram interims flow, but the turn stops
    # before any is_final (Silero silence between the wake burst and the rest).
    state.turn_type = "wake_word"
    state.interims.record("Alexa, good evening. Nice to meet you.")
    observer._route_audio(b"W" * 320)
    observer._route_audio(b"U" * 320)
    await observer._on_user_stopped()

    assert made == [], "a no-transcript turn must not create a phantom storage"
    assert bytes(observer._turn_buf) == b"W" * 320 + b"U" * 320, (
        "the audio must carry into the following turn, not be dropped"
    )
    assert len(state.interims) == 1, "interims must not be abandoned by the phantom turn"

    # The real turn finalizes → storage is created, and the TTS bridge's mark
    # (mark_stt_final) sees the full text + the preserved interims + audio.
    observer._route_audio(b"V" * 320)
    observer._saw_text = True
    await observer._on_user_stopped()

    assert len(made) == 1
    storage = made[0]
    storage.mark_stt_final("good evening. Nice to meet you.")
    assert storage.stt_text == "good evening. Nice to meet you."
    assert storage.stt_status == "final"
    assert storage.stt_interims == ["Alexa, good evening. Nice to meet you."]
    assert storage.user_pcm_bytes == b"W" * 320 + b"U" * 320 + b"V" * 320


@pytest.mark.asyncio
async def test_observer_tts_played_ack_lifetime():
    """tts_played acks are accepted while the reply's sentence id is live (even
    after the server finished sending — the device acks on first DAC output,
    which lags send-complete; this is the 'late tts_played' drop fix), and
    dropped once the next user turn resets current_sentence_id, so a stale ack
    for a finished reply cannot bleed into the next turn's telemetry."""
    state = PiguguTurnState()
    observer = PiguguTurnStorageObserver(
        None, state, session_id="s1", client_id="d1", user_id="u1"
    )

    # Reply sentence 1 is live (still playing on the device): matching ack
    # accepted — this is the ack that used to arrive after send-complete and
    # was dropped because current_sentence_id had already been zeroed.
    state.current_sentence_id = 1
    await observer._on_device_message(
        {"type": "listen", "state": "tts_played", "sentence_id": 1, "device_playback_ms": 120}
    )
    assert state.device_playback_ms == 120

    # A stale ack for a DIFFERENT sentence is rejected.
    await observer._on_device_message(
        {"type": "listen", "state": "tts_played", "sentence_id": 9, "device_playback_ms": 500}
    )
    assert state.device_playback_ms == 120

    # Turn boundary: the observer resets current_sentence_id + device_playback_ms.
    # A late ack for the OLD sentence must not resurrect the value into the
    # next turn's telemetry.
    state.current_sentence_id = 0
    state.device_playback_ms = 0
    await observer._on_device_message(
        {"type": "listen", "state": "tts_played", "sentence_id": 1, "device_playback_ms": 900}
    )
    assert state.device_playback_ms == 0
