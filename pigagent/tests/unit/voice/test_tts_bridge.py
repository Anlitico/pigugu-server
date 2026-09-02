"""Unit tests for the TTS bridge interrupt-text truncation.

An interrupted (barge-in) reply must not record the full generated text as
"spoken": the storage record and the LLM context should only carry the portion
whose audio actually reached the device. Covers ``_spoken_prefix`` (the exact
word-timestamp cut, pipecat pattern) plus ``_truncate_to_played`` (the
sent/synthesized ratio fallback for providers without word timestamps), and the
bridge-level interrupt path (storage mark + ctx persistence).
"""

import asyncio

import pytest
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.asyncio.task_manager import TaskManager

from voice.interims import InterimBuffer
from voice.pipecat.state import PiguguTurnState
from voice.pipecat.tts_bridge import PiguguTtsBridge
from voice.storage import TurnStorage

# 16 kHz mono int16 bytes per second.
_PCM_BYTES_PER_SEC = 16000 * 2

CHUNK1 = "First complete sentence here. Second complete sentence here. "
CHUNK2 = "Third sentence more text. Fourth and final sentence."

# Sentence-level split of CHUNK1, for fakes that die mid-chunk.
CHUNK1_SENT1 = "First complete sentence here. "
CHUNK1_SENT2 = "Second complete sentence here. "

# Word-timestamp events Cartesia would report for CHUNK1 (8 words over 600ms).
CHUNK1_WORDS = [
    ("First", 0.0, 0.075),
    ("complete", 0.075, 0.15),
    ("sentence", 0.15, 0.225),
    ("here.", 0.225, 0.3),
    ("Second", 0.3, 0.375),
    ("complete", 0.375, 0.45),
    ("sentence", 0.45, 0.525),
    ("here.", 0.525, 0.6),
]

# Same for CHUNK2, spanning the next 600ms of audio time.
CHUNK2_WORDS = [
    ("Third", 0.6, 0.675),
    ("sentence", 0.675, 0.75),
    ("more", 0.75, 0.825),
    ("text.", 0.825, 0.9),
    ("Fourth", 0.9, 0.975),
    ("and", 0.975, 1.05),
    ("final", 1.05, 1.125),
    ("sentence.", 1.125, 1.2),
]


class _FakeCtx:
    def __init__(self):
        self.turns: list[tuple[str, str, bool]] = []

    async def add_turn(self, role: str, content: str, *, partial: bool = False, **kwargs) -> None:
        self.turns.append((role, content, partial))


# Word timestamps Cartesia would report for the FIRST sentence of CHUNK1.
CHUNK1_SENT1_WORDS = [
    ("First", 0.0, 0.075),
    ("complete", 0.075, 0.15),
    ("sentence", 0.15, 0.225),
    ("here.", 0.225, 0.3),
]


class _FakePig:
    model = "fake-llm"

    def __init__(self):
        self.ctx = _FakeCtx()

    async def generate_reply(self, user_text, *, persona_id=1, interrupt_event=None, session_id=None):
        yield CHUNK1
        yield CHUNK2


class _SentencePig:
    """Like ``_FakePig`` but yields CHUNK1 sentence-by-sentence, so a fake WS
    can die mid-chunk with only the first sentence consumed."""

    model = "fake-llm"

    def __init__(self):
        self.ctx = _FakeCtx()

    async def generate_reply(self, user_text, *, persona_id=1, interrupt_event=None, session_id=None):
        yield CHUNK1_SENT1
        yield CHUNK1_SENT2
        yield CHUNK2


class _BargeInTTS:
    """Streams 600ms of audio per consumed chunk (with CHUNK1's word-timestamp
    events), then fires the barge-in so the second chunk never plays."""

    FRAMES_PER_CHUNK = 10  # 10 × 60ms = 600ms

    def __init__(self, state):
        self._state = state

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        first = True
        async for text in text_source:
            if interrupt_event.is_set():
                break
            pcm = bytes(self.FRAMES_PER_CHUNK * int(60 * _PCM_BYTES_PER_SEC / 1000))
            if collect_pcm is not None:
                collect_pcm.extend(pcm)
            if collect_words is not None and first:
                collect_words.extend(CHUNK1_WORDS)
            yield [b"opus-frame"] * self.FRAMES_PER_CHUNK
            if first:
                first = False
                self._state.interrupt_event.set()


class _FullSynthesisTTS:
    """Simulates the e0d34a17 failure mode: Cartesia synthesizes the WHOLE
    reply (both chunks' words reported), but the server only sends the first
    600ms before the barge-in. The word-timestamp cut must keep only the
    words whose audio actually reached the device."""

    FRAMES_PER_CHUNK = 10  # 10 × 60ms = 600ms

    def __init__(self, state):
        self._state = state

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        first = True
        async for text in text_source:
            if interrupt_event.is_set():
                break
            pcm = bytes(self.FRAMES_PER_CHUNK * int(60 * _PCM_BYTES_PER_SEC / 1000))
            if collect_pcm is not None:
                collect_pcm.extend(pcm)
            if collect_words is not None and first:
                # Full synthesis: words for BOTH chunks.
                collect_words.extend(CHUNK1_WORDS + CHUNK2_WORDS)
            yield [b"opus-frame"] * self.FRAMES_PER_CHUNK
            if first:
                first = False
                self._state.interrupt_event.set()


class _RestAfterPrefixTTS:
    """The WS phase consumes the first sentence and reports its word
    timestamps, then dies before anything plays; the bridge falls back to a
    whole-reply REST synthesis. A barge-in during REST playback must cut from
    the FULL text — the WS-prefix words describe audio that never played."""

    FRAMES_PER_CHUNK = 40  # REST: 40 × 60ms = 2.4s → real-time pacing during send

    def __init__(self, state):
        self._state = state

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        if False:  # pragma: no cover — unreachable, but makes this an async generator
            yield
        # Consume the first chunk (records it in the bridge's `consumed` list)
        # and report its words, then die before producing any audio.
        await text_source.__anext__()
        if collect_words is not None:
            collect_words.extend(CHUNK1_SENT1_WORDS)
        raise RuntimeError("ws stream died mid-chunk")

    async def synthesize(self, text, *, collect_pcm=None, **kwargs):
        pcm = bytes(self.FRAMES_PER_CHUNK * int(60 * _PCM_BYTES_PER_SEC / 1000))
        if collect_pcm is not None:
            collect_pcm.extend(pcm)
        return [b"opus-frame"] * self.FRAMES_PER_CHUNK


class _RestCancelTTS:
    """WS dies before playback → REST fallback; synthesize() fills the PCM and
    schedules a bare cancel (no interrupt event) to land mid-playback — a
    disconnect hitting the fallback. The cancelled reply must be recorded as
    interrupted, never as a complete full reply."""

    FRAMES_PER_CHUNK = 40  # 2.4s → real-time pacing so the cancel lands mid-send

    def __init__(self, state):
        self._state = state

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        if False:  # pragma: no cover — unreachable, but makes this an async generator
            yield
        raise RuntimeError("ws stream died before playback")

    async def synthesize(self, text, *, collect_pcm=None, **kwargs):
        pcm = bytes(self.FRAMES_PER_CHUNK * int(60 * _PCM_BYTES_PER_SEC / 1000))
        if collect_pcm is not None:
            collect_pcm.extend(pcm)
        # Deliver the bare cancel while _send_batch/_pace is still running.
        task = asyncio.current_task()

        async def _cancel_later():
            await asyncio.sleep(0.02)
            task.cancel()

        asyncio.ensure_future(_cancel_later())
        return [b"opus-frame"] * self.FRAMES_PER_CHUNK


class _RestFallbackTTS:
    """Simulates the WS stream dying before any playback; the bridge falls
    back to one whole-reply REST synthesis. A barge-in then cuts the REST
    playback short — the recorded text must be a truncated prefix of the FULL
    reply (H1: the fallback base is ``holder["full"]``, never the empty
    ``consumed`` list)."""

    FRAMES_PER_CHUNK = 40  # 40 × 60ms = 2.4s → real-time pacing during send

    def __init__(self, state):
        self._state = state

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        if False:  # pragma: no cover — unreachable, but makes this an async generator
            yield
        raise RuntimeError("ws stream died before playback")

    async def synthesize(self, text, *, collect_pcm=None, **kwargs):
        pcm = bytes(self.FRAMES_PER_CHUNK * int(60 * _PCM_BYTES_PER_SEC / 1000))
        if collect_pcm is not None:
            collect_pcm.extend(pcm)
        return [b"opus-frame"] * self.FRAMES_PER_CHUNK


class _FullPlayTTS:
    """Streams the whole reply with no interrupt — lets the bridge finish
    sending and reach the playback drain."""

    FRAMES_PER_CHUNK = 10  # 10 × 60ms = 600ms

    def __init__(self, state):
        self._state = state

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        async for text in text_source:
            if interrupt_event.is_set():
                break
            pcm = bytes(self.FRAMES_PER_CHUNK * int(60 * _PCM_BYTES_PER_SEC / 1000))
            if collect_pcm is not None:
                collect_pcm.extend(pcm)
            yield [b"opus-frame"] * self.FRAMES_PER_CHUNK


def _make_bridge(pig=None, tts=None, state=None, task_manager=None):
    return PiguguTtsBridge(
        pig if pig is not None else _FakePig(),
        tts if tts is not None else _BargeInTTS(PiguguTurnState()),
        state=state if state is not None else PiguguTurnState(),
        session_id="test-session",
        task_manager=task_manager,
    )


def _make_storage():
    return TurnStorage(
        turn_id="t1",
        session_id="test-session",
        turn_idx=1,
        device_id="dev-1",
        user_id="user-1",
        persona_id=1,
        utc_start_ms=0,
        s3_bucket="bucket",
        s3_prefix="voice-turns",
        clickhouse_dsn="clickhouse://u:p@h:9000/voice",
        clickhouse_table="voice.turns",
        interims=InterimBuffer(),
        voice_chunk_flags_slice=lambda: [],
    )


# ── _truncate_to_played ───────────────────────────────────────────────


def test_truncate_nothing_sent_is_empty():
    b = _make_bridge()
    b._play_position = 0.0
    assert b._truncate_to_played(CHUNK1 + CHUNK2, bytes(2 * _PCM_BYTES_PER_SEC)) == ""


def test_truncate_no_synthesis_is_empty():
    b = _make_bridge()
    b._play_position = 1.0
    assert b._truncate_to_played(CHUNK1 + CHUNK2, b"") == ""


def test_truncate_empty_text_is_empty():
    b = _make_bridge()
    assert b._truncate_to_played("  ", bytes(2 * _PCM_BYTES_PER_SEC)) == ""


def test_truncate_full_ratio_keeps_text():
    b = _make_bridge()
    b._play_position = 2.0
    text = CHUNK1 + CHUNK2
    assert b._truncate_to_played(text, bytes(2 * _PCM_BYTES_PER_SEC)) == text.strip()


def test_truncate_snaps_to_sentence_boundary():
    b = _make_bridge()
    # 1s sent of 2s synthesized → half the text, snapped back to the first
    # sentence boundary.
    b._play_position = 1.0
    text = "Alpha first sentence here. Beta second one. Gamma third one."
    out = b._truncate_to_played(text, bytes(2 * _PCM_BYTES_PER_SEC))
    assert out == "Alpha first sentence here."


def test_truncate_mid_sentence_returns_fragment():
    b = _make_bridge()
    # 30% of 1s → cut lands mid first sentence, no boundary → the fragment.
    b._play_position = 0.3
    text = "Alpha first sentence here. Beta second one."
    out = b._truncate_to_played(text, bytes(_PCM_BYTES_PER_SEC))
    assert out == "Alpha first"


def test_truncate_ratio_capped_at_one():
    b = _make_bridge()
    b._play_position = 5.0  # more sent than synthesized
    text = CHUNK1 + CHUNK2
    assert b._truncate_to_played(text, bytes(2 * _PCM_BYTES_PER_SEC)) == text.strip()


# ── _spoken_prefix (word-timestamp cut) ───────────────────────────────


def test_spoken_prefix_all_words_sent():
    b = _make_bridge()
    words = [("Alpha", 0.0, 0.2), ("first", 0.2, 0.4), ("sentence.", 0.4, 0.6)]
    assert b._spoken_prefix(words, 0.6) == "Alpha first sentence."


def test_spoken_prefix_cuts_at_word_boundary():
    b = _make_bridge()
    words = [
        ("Alpha", 0.0, 0.2),
        ("first", 0.2, 0.4),
        ("sentence.", 0.4, 0.6),
        ("Beta", 0.6, 0.8),
    ]
    # Sent 0.55s → "sentence." ends at 0.6 (>0.55) → dropped; keep up to "first".
    assert b._spoken_prefix(words, 0.55) == "Alpha first"


def test_spoken_prefix_in_progress_word_dropped():
    b = _make_bridge()
    words = [("Alpha", 0.0, 0.2), ("first", 0.2, 0.4), ("sentence.", 0.4, 0.6)]
    # Cut lands inside "first" (0.3s): its end 0.4 > 0.3 → dropped, keep "Alpha".
    assert b._spoken_prefix(words, 0.3) == "Alpha"


def test_spoken_prefix_nothing_sent_is_empty():
    b = _make_bridge()
    assert b._spoken_prefix([("Alpha", 0.0, 0.2)], 0.0) == ""


# ── bridge-level interrupt path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_interrupt_persists_only_spoken_portion():
    state = PiguguTurnState()
    storage = _make_storage()
    state.turn_storage = storage
    pig = _FakePig()
    bridge = _make_bridge(
        pig=pig, tts=_BargeInTTS(state), state=state, task_manager=TaskManager()
    )
    stt_context: list[str] = []
    bridge._stt_context_cb = lambda text: stt_context.append(text)
    # The bridge has no pipecat pipeline — neutralize wire + bot frames.
    async def _noop_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    bridge.push_frame = _noop_push

    await bridge._run_tts("hello there")

    assert storage.tts_text == CHUNK1.strip() + " [The user interrupted me.]"
    assert storage.tts_status == "interrupted"
    assert storage.tts_truncated_reason == "barge_in"
    # Only chunk1's 600ms of audio was synthesized and sent.
    assert len(storage.tts_pcm_buf) == _BargeInTTS.FRAMES_PER_CHUNK * int(60 * _PCM_BYTES_PER_SEC / 1000)

    # Let the fire-and-forget ctx tasks land.
    await asyncio.sleep(0.01)
    turns = pig.ctx.turns
    # The user turn is persisted as usual.
    assert ("user", "hello there", False) in turns
    # The assistant reply is the SPOKEN portion only — never the full reply —
    # and is explicitly marked as interrupted (inline tag + partial flag) so
    # the LLM knows it was cut off.
    assistant = [(c, p) for r, c, p in turns if r == "assistant"]
    assert assistant == [(CHUNK1.strip() + " [The user interrupted me.]", True)]
    assert all("Third sentence" not in c for c, _ in assistant)
    # A partial sentence is never used to hint the STT decoder.
    assert stt_context == []


@pytest.mark.asyncio
async def test_interrupt_full_synthesis_keeps_only_sent_words():
    """e0d34a17 scenario: Cartesia reported the whole reply's words but only
    600ms was sent. The word-timestamp cut keeps CHUNK1's words and drops
    CHUNK2 entirely — the full reply never reaches storage or ctx."""
    state = PiguguTurnState()
    storage = _make_storage()
    state.turn_storage = storage
    pig = _FakePig()
    bridge = _make_bridge(
        pig=pig, tts=_FullSynthesisTTS(state), state=state, task_manager=TaskManager()
    )

    async def _noop_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    bridge.push_frame = _noop_push

    await bridge._run_tts("hello there")

    # Only CHUNK1's words end ≤ the 600ms sent — CHUNK2's are all dropped.
    assert storage.tts_text == CHUNK1.strip() + " [The user interrupted me.]"
    assert storage.tts_status == "interrupted"

    await asyncio.sleep(0.01)
    assistant = [(c, p) for r, c, p in pig.ctx.turns if r == "assistant"]
    assert assistant == [(CHUNK1.strip() + " [The user interrupted me.]", True)]
    assert all("Third" not in c and "Fourth" not in c for c, _ in assistant)


@pytest.mark.asyncio
async def test_interrupt_before_audio_writes_no_assistant():
    state = PiguguTurnState()
    storage = _make_storage()
    state.turn_storage = storage
    pig = _FakePig()
    bridge = _make_bridge(
        pig=pig, tts=_BargeInTTS(state), state=state, task_manager=TaskManager()
    )
    bridge._play_position = 0.0  # nothing reached the device
    state.interrupt_event.set()  # barge-in before the first frame

    async def _noop_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    bridge.push_frame = _noop_push

    await bridge._run_tts("hello there")

    assert storage.tts_text == ""
    assert storage.tts_status == "empty"

    await asyncio.sleep(0.01)
    assistant = [c for r, c, p in pig.ctx.turns if r == "assistant"]
    assert assistant == []


@pytest.mark.asyncio
async def test_interrupt_rest_fallback_truncates_full_reply():
    """H1: the WS stream died before any playback, so the bridge re-synthesized
    the WHOLE reply via REST and played part of it before a barge-in. ``consumed``
    is empty on that path — the ratio cut must be based on ``holder["full"]``,
    or an interrupted REST reply would record nothing despite audio reaching
    the device."""
    state = PiguguTurnState()
    storage = _make_storage()
    state.turn_storage = storage
    pig = _FakePig()
    bridge = _make_bridge(
        pig=pig, tts=_RestFallbackTTS(state), state=state, task_manager=TaskManager()
    )

    async def _noop_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    bridge.push_frame = _noop_push

    async def _barge_mid_playback():
        # Fire when the playback clock reports ~1.5s of the 2.4s reply sent —
        # deterministic regardless of wall-clock speed (a busy CI delaying a
        # fixed-sleep barge past pace completion would otherwise flake).
        for _ in range(500):  # ~2.5s of polling max
            if bridge._play_position >= 1.5:
                break
            await asyncio.sleep(0.005)
        state.interrupt_event.set()

    barge = asyncio.create_task(_barge_mid_playback())
    await bridge._run_tts("hello there")
    await barge

    # A non-empty truncated prefix of the full reply — never "" (the pre-fix
    # H1 bug) and never the unheard tail of the reply.
    assert storage.tts_text.startswith("First")
    assert storage.tts_text.endswith(" [The user interrupted me.]")
    assert "Fourth" not in storage.tts_text
    assert storage.tts_status == "interrupted"
    assert storage.tts_truncated_reason == "barge_in"

    await asyncio.sleep(0.01)
    assistant = [(c, p) for r, c, p in pig.ctx.turns if r == "assistant"]
    assert assistant and assistant[0][1] is True
    assert "Fourth" not in assistant[0][0]


@pytest.mark.asyncio
async def test_interrupt_rest_fallback_ignores_ws_prefix_words():
    """H2: the WS phase consumed the first sentence (reporting its word
    timestamps) before dying, so the bridge re-synthesized the WHOLE reply via
    REST and played most of it before a barge-in. The cut must be based on the
    FULL reply text — the WS-prefix words describe audio that never played and
    must not limit the recorded portion (a tiny first-sentence fragment)."""
    state = PiguguTurnState()
    storage = _make_storage()
    state.turn_storage = storage
    pig = _SentencePig()
    bridge = _make_bridge(
        pig=pig, tts=_RestAfterPrefixTTS(state), state=state, task_manager=TaskManager()
    )

    async def _noop_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    bridge.push_frame = _noop_push

    async def _barge_mid_playback():
        # Fire when the playback clock reports ~1.5s of the 2.4s reply sent —
        # deterministic regardless of wall-clock speed (see the H1 test).
        for _ in range(500):
            if bridge._play_position >= 1.5:
                break
            await asyncio.sleep(0.005)
        state.interrupt_event.set()

    barge = asyncio.create_task(_barge_mid_playback())
    await bridge._run_tts("hello there")
    await barge

    # Cut from the FULL reply: ~50% of 99 chars lands in the second sentence.
    # Pre-fix the WS-prefix words would limit this to "First complete sentence
    # here." — no "Second".
    assert "Second" in storage.tts_text
    assert storage.tts_text.endswith(" [The user interrupted me.]")
    assert "Fourth" not in storage.tts_text
    assert storage.tts_status == "interrupted"
    assert storage.tts_truncated_reason == "barge_in"

    await asyncio.sleep(0.01)
    assistant = [(c, p) for r, c, p in pig.ctx.turns if r == "assistant"]
    assert assistant and assistant[0][1] is True
    assert "Fourth" not in assistant[0][0]


@pytest.mark.asyncio
async def test_event_during_drain_records_interrupted():
    """R3-high: a barge-in landing while the finally drains the tail playback
    must record the reply as interrupted (marker + partial), never as a
    complete full reply. Exercises the drain returning False with the event
    set (no cancel involved) — the 'drain completed' reading would store the
    whole text the user never heard."""
    state = PiguguTurnState()
    storage = _make_storage()
    state.turn_storage = storage
    pig = _FakePig()
    bridge = _make_bridge(
        pig=pig, tts=_FullPlayTTS(state), state=state, task_manager=TaskManager()
    )

    async def _noop_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    bridge.push_frame = _noop_push

    task = asyncio.create_task(bridge._run_tts("hello there"))
    # Give the send time to finish; the finally is now waiting out the drain.
    await asyncio.sleep(0.05)
    # A barge-in fires without cancelling the task — the drain wakes via the
    # event and must be treated as an interruption.
    state.interrupt_event.set()
    await task

    assert storage.tts_status == "interrupted"
    assert storage.tts_truncated_reason == "barge_in"
    assert storage.tts_text.endswith(" [The user interrupted me.]")

    await asyncio.sleep(0.01)
    assistant = [(c, p) for r, c, p in pig.ctx.turns if r == "assistant"]
    assert assistant and assistant[0][1] is True


@pytest.mark.asyncio
async def test_cancel_during_rest_fallback_records_interrupted():
    """R4-M2: a bare cancel landing inside the REST fallback (disconnect /
    shutdown) must not be recorded as a complete delivered reply. The
    CancelledError raised in the ``except RuntimeError`` handler body is not
    caught by the sibling CancelledError handler — without the nested guard
    the partial reply would commit as ``complete`` with no marker."""
    state = PiguguTurnState()
    storage = _make_storage()
    state.turn_storage = storage
    pig = _FakePig()
    bridge = _make_bridge(
        pig=pig, tts=_RestCancelTTS(state), state=state, task_manager=TaskManager()
    )

    async def _noop_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    bridge.push_frame = _noop_push

    with pytest.raises(asyncio.CancelledError):
        await bridge._run_tts("hello there")

    assert storage.tts_status == "interrupted"
    assert storage.tts_truncated_reason == "cancelled"
    assert storage.tts_text.endswith(" [The user interrupted me.]")

    await asyncio.sleep(0.01)
    assistant = [(c, p) for r, c, p in pig.ctx.turns if r == "assistant"]
    assert assistant and assistant[0][1] is True


@pytest.mark.asyncio
async def test_cancel_during_drain_still_persists_interrupted():
    """M1: a bare cancel while the finally block is draining playback must not
    abort the finally — the interruption is recorded (storage + ctx) instead of
    the whole write path being skipped."""
    state = PiguguTurnState()
    storage = _make_storage()
    state.turn_storage = storage
    pig = _FakePig()
    bridge = _make_bridge(
        pig=pig, tts=_FullPlayTTS(state), state=state, task_manager=TaskManager()
    )

    async def _noop_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    bridge.push_frame = _noop_push

    task = asyncio.create_task(bridge._run_tts("hello there"))
    # Give the send time to finish; the finally is now waiting out the drain.
    await asyncio.sleep(0.05)
    task.cancel()
    # The finally swallowed the cancel: the task completes normally and the
    # interrupted record still lands.
    await task

    assert storage.tts_status == "interrupted"
    assert storage.tts_text.endswith(" [The user interrupted me.]")
    assert storage.tts_text.startswith("First")
    assert storage.tts_truncated_reason == "cancelled"

    await asyncio.sleep(0.01)
    assistant = [(c, p) for r, c, p in pig.ctx.turns if r == "assistant"]
    assert assistant and assistant[0][1] is True
