"""Unit tests for merging a replyless turn into the next turn that speaks.

The STT provider ends a turn on a sentence boundary, so one multi-sentence
utterance can arrive as several turns. When the next split lands while the
previous reply is still in the LLM/TTS warm-up, that reply is killed before a
single frame plays: the user hears nothing for it. Left alone, those turns sit
in the history as standalone user fragments, each with a reply nobody heard.

The TTS bridge holds such a turn's words back and prepends them to the next
turn that does speak, so the LLM answers the whole utterance in one go and the
history holds one user message instead of a run of fragments.
"""

import asyncio

import pytest
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.asyncio.task_manager import TaskManager

from voice.interims import InterimBuffer
from voice.pipecat.state import PiguguTurnState
from voice.pipecat.tts_bridge import _PENDING_TEXT_FLUSH_CHARS, PiguguTtsBridge
from voice.storage import TurnStorage

REPLY = "Sure, here is the answer."


class _FakeCtx:
    def __init__(self):
        self.turns: list[tuple[str, str, bool]] = []

    async def add_turn(self, role: str, content: str, *, partial: bool = False, **kwargs) -> None:
        self.turns.append((role, content, partial))


class _RecordingPig:
    """Records the prompt each turn was answered from."""

    model = "fake-llm"

    def __init__(self):
        self.ctx = _FakeCtx()
        self.prompts: list[str] = []

    async def generate_reply(self, user_text, *, persona_id=1, interrupt_event=None, session_id=None):
        self.prompts.append(user_text)
        yield REPLY


class _ScriptedTTS:
    """Per turn, in order: either play the reply out, or be killed before any
    audio (the barge-in that split the utterance lands during warm-up)."""

    def __init__(self, voiced: list[bool]):
        self._voiced = list(voiced)

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        if not self._voiced.pop(0):
            interrupt_event.set()
            return
        async for _text in text_source:
            if interrupt_event.is_set():
                break
            yield [b"opus-frame"] * 5


class _ParkedThenVoicedTTS:
    """First turn parks in warm-up and never returns — it is cancelled, which is
    what a barge-in does to a replyless turn in production (``_abort`` cancels
    the task rather than merely setting the interrupt). The second plays out."""

    def __init__(self):
        self.parked = asyncio.Event()
        self._calls = 0

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None, collect_words=None):
        self._calls += 1
        if self._calls == 1:
            self.parked.set()
            await asyncio.Event().wait()
            return
        async for _text in text_source:
            if interrupt_event.is_set():
                break
            yield [b"opus-frame"] * 5


class _NoCtxPig:
    """No ctx — the backend-less case the ctx guards must tolerate."""

    model = "fake-llm"

    def __init__(self):
        self.ctx = None

    async def generate_reply(self, user_text, *, persona_id=1, interrupt_event=None, session_id=None):
        yield REPLY


def _make_bridge_with(pig, tts, state):
    bridge = PiguguTtsBridge(
        pig,
        tts,
        state=state,
        session_id="test-session",
        task_manager=TaskManager(),
    )

    async def _noop_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    bridge.push_frame = _noop_push
    return bridge


def _make_bridge(pig, voiced, state):
    return _make_bridge_with(pig, _ScriptedTTS(voiced), state)


async def _run_turn(bridge, text: str):
    """One full turn through the real entry point (``_begin_turn`` resets the
    interrupt and supersedes the previous reply exactly as the pipeline does)."""
    await bridge._begin_turn(text)
    await bridge._tts_task


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


# ── the merge ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replyless_turn_is_held_back_and_answered_by_the_next():
    state = PiguguTurnState()
    pig = _RecordingPig()
    bridge = _make_bridge(pig, voiced=[False, True], state=state)

    await _run_turn(bridge, "First half of the question.")
    assert state.pending_user_text == "First half of the question."

    await _run_turn(bridge, "Second half of the question.")

    # The fragment's words reach the LLM a second time inside the merged prompt,
    # so the reply answers the whole utterance. (The doomed first turn did call
    # the LLM with the fragment alone — what this change fixes is what lands in
    # the history, not that wasted call.)
    assert pig.prompts == [
        "First half of the question.",
        "First half of the question. Second half of the question.",
    ]

    await asyncio.sleep(0.01)
    # One user message, then its reply — not two user messages with a reply
    # the user never heard.
    assert pig.ctx.turns == [
        ("user", "First half of the question. Second half of the question.", False),
        ("assistant", REPLY, False),
    ]
    assert state.pending_user_text == ""


@pytest.mark.asyncio
async def test_a_run_of_replyless_turns_merges_into_one_message():
    state = PiguguTurnState()
    pig = _RecordingPig()
    bridge = _make_bridge(pig, voiced=[False, False, True], state=state)

    await _run_turn(bridge, "A.")
    await _run_turn(bridge, "B.")
    await _run_turn(bridge, "C.")

    assert pig.prompts[-1] == "A. B. C."
    await asyncio.sleep(0.01)
    assert [role for role, _, _ in pig.ctx.turns] == ["user", "assistant"]
    assert pig.ctx.turns[0][1] == "A. B. C."


@pytest.mark.asyncio
async def test_a_cancelled_turn_still_hands_its_text_on():
    """The production shape of a replyless turn: the barge-in CANCELS the task
    (``_abort``), it does not merely set the interrupt. The finally then runs
    with a pending CancelledError, and the hand-off must survive that."""
    state = PiguguTurnState()
    pig = _RecordingPig()
    tts = _ParkedThenVoicedTTS()
    bridge = _make_bridge_with(pig, tts, state)

    await bridge._begin_turn("First half of the question.")
    await tts.parked.wait()
    bridge._tts_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await bridge._tts_task

    assert state.pending_user_text == "First half of the question."

    await _run_turn(bridge, "Second half of the question.")
    assert pig.prompts[-1] == (
        "First half of the question. Second half of the question."
    )
    await asyncio.sleep(0.01)
    assert [role for role, _, _ in pig.ctx.turns] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_a_very_long_run_is_written_out_instead_of_growing_the_prompt():
    """If the replies keep being killed, the held text must not accumulate for
    the whole session into one unbounded merged prompt — it is outside ctx, so
    compression cannot trim it either."""
    state = PiguguTurnState()
    pig = _RecordingPig()
    bridge = _make_bridge(pig, voiced=[False, False], state=state)

    # Two turns of just over half the cap each must trip it.
    chunk = "x" * (_PENDING_TEXT_FLUSH_CHARS // 2 + 10)

    await _run_turn(bridge, chunk)
    assert state.pending_user_text == chunk  # one chunk alone is still under

    await _run_turn(bridge, chunk)
    await asyncio.sleep(0.01)
    assert state.pending_user_text == ""
    assert [role for role, _, _ in pig.ctx.turns] == ["user"]
    assert len(pig.ctx.turns[0][1]) >= _PENDING_TEXT_FLUSH_CHARS


@pytest.mark.asyncio
async def test_a_voiced_turn_writes_its_own_user_record():
    """A turn the user actually heard keeps its own record."""
    state = PiguguTurnState()
    pig = _RecordingPig()
    bridge = _make_bridge(pig, voiced=[True], state=state)

    await _run_turn(bridge, "What is the weather?")

    await asyncio.sleep(0.01)
    assert pig.ctx.turns == [
        ("user", "What is the weather?", False),
        ("assistant", REPLY, False),
    ]


@pytest.mark.asyncio
async def test_storage_keeps_the_turns_own_transcript_not_the_merged_text():
    """The merge is for the LLM and the history only: the per-turn sidecar
    must keep recording what was heard in that turn, or ``stt_text`` would
    double-count the carried fragments."""
    state = PiguguTurnState()
    pig = _RecordingPig()
    bridge = _make_bridge(pig, voiced=[False, True], state=state)

    await _run_turn(bridge, "First half.")
    storage = _make_storage()
    state.turn_storage = storage
    await _run_turn(bridge, "Second half.")

    assert storage.stt_text == "Second half."
    await asyncio.sleep(0.01)
    assert pig.ctx.turns[0][1] == "First half. Second half."


# ── session end ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_end_writes_the_held_back_text():
    state = PiguguTurnState()
    pig = _RecordingPig()
    bridge = _make_bridge(pig, voiced=[False], state=state)

    await _run_turn(bridge, "Never answered.")
    await asyncio.sleep(0.01)
    assert pig.ctx.turns == []

    await bridge.flush_pending_user_text()
    assert pig.ctx.turns == [("user", "Never answered.", False)]
    assert state.pending_user_text == ""

    # Idempotent: a second flush (e.g. cleanup racing a reconnect) writes
    # nothing more.
    await bridge.flush_pending_user_text()
    assert len(pig.ctx.turns) == 1


@pytest.mark.asyncio
async def test_session_end_flush_is_a_noop_without_a_ctx():
    state = PiguguTurnState()
    bridge = _make_bridge(_NoCtxPig(), voiced=[False], state=state)

    await _run_turn(bridge, "Never answered.")
    await bridge.flush_pending_user_text()

    assert state.pending_user_text == ""
