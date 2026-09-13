"""Turn → agent handoff: merges the turn transcript, emits one user-turn frame.

Grows out of M2's aggregator: on ``UserStoppedSpeakingFrame`` the accumulated
``TranscriptionFrame`` texts become ONE ``PiguguUserTurnFrame`` handed to the
TTS bridge (which runs the LLM + TTS). ``on_turn`` is kept for tests and
observers. This is the 6be1be41 fix made visible — one user utterance,
however many Deepgram ``is_final`` chunks it produced, is ONE turn.

Deliberately does NOT reset the text buffer on ``UserStartedSpeakingFrame``: that
broadcast is async and can race the transcription frames. Every turn ends with a
``UserStoppedSpeakingFrame`` (stop strategy or watchdog), which is the only place
the buffer is cleared — so turn boundaries stay correct regardless of frame
ordering. That frame is handled, but only to cancel a pending wake ack.
"""

from __future__ import annotations

import asyncio
import re

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserIdleTimeoutUpdateFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.pipecat.pigugu_serializer import (
    PiguguMessageFrame,
    PiguguOutputMessageFrame,
    PiguguUserTurnFrame,
)

# WakeNet names arrive glued together: the firmware reports the model's wake
# word via esp_wn_wakeword_from_name, which for wn10_heypigugu is "heypigugu".
# Split a leading greeting off the rest so the turn reads as the utterance the
# user actually made ("hey pigugu"); anything already spaced, or not matching a
# known greeting, is left as it is. Longest first, so "okay" is not split as
# "ok" + "ay".
_WAKE_WORD_GREETINGS = ("hello", "okay", "hey", "hi", "ok")


def normalize_wake_word(raw: str) -> str:
    """Readable form of the wake word the firmware reports."""
    word = " ".join((raw or "").split())
    if not word or " " in word:
        return word
    lower = word.lower()
    for greeting in _WAKE_WORD_GREETINGS:
        if lower.startswith(greeting) and len(word) > len(greeting):
            return f"{word[:len(greeting)]} {word[len(greeting):]}"
    return word


def _leads_with_wake_word(text_lower: str, word_lower: str) -> bool:
    """True when the transcript already begins with the wake word.

    Tolerates what an STT adds to it: punctuation ("hey pigugu, how are you",
    "hey. pigugu …"), the glued single-token form the firmware itself uses
    ("heypigugu"), and full-width punctuation. A word-boundary match keeps a
    different word that merely shares the opening letters ("hiking trails" vs
    "hi") out of it.
    """
    if text_lower == word_lower:
        return True
    if re.match(rf"{re.escape(word_lower)}\b", text_lower):
        return True
    # Punctuation the STT inserted inside the wake word itself ("hey. pigugu",
    # "hey  pigugu"): dropping it and collapsing whitespace re-forms the phrase,
    # in either the spaced or the glued shape, and the boundary check still
    # applies. The word-boundary guard is what keeps "hiking trails" out.
    glued = word_lower.replace(" ", "")
    flat = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text_lower)).strip()
    if flat != text_lower and (
        re.match(rf"{re.escape(word_lower)}\b", flat)
        or re.match(rf"{re.escape(glued)}\b", flat)
    ):
        return True
    head = re.split(r"[\s,.!?;:，。！？；：、]+", text_lower, maxsplit=1)[0]
    return head == glued


def wake_turn_text(text: str, wake_word: str) -> str:
    """The wake turn's user text: the wake word, then what the user said.

    Pure so both wake-turn paths share it — the transcript-driven one (the
    gateway below, when the user spoke on) and the synthesised bare-wake one
    (no transcript at all, so the text is the wake word alone).
    """
    word = normalize_wake_word(wake_word)
    rest = (text or "").strip()
    if not word:
        return rest
    # Idempotent: a build with CONFIG_SEND_WAKE_WORD_DATA=y streams the wake-word
    # audio, so its transcript already starts with the wake word — prepending
    # again would read "hey pigugu hey pigugu …".
    if _leads_with_wake_word(rest.lower(), word.lower()):
        return rest
    return f"{word} {rest}" if rest else word


class PiguguAgentGateway(FrameProcessor):
    """Accumulate turn transcript; emit one ``PiguguUserTurnFrame`` on turn end."""

    def __init__(
        self,
        on_turn=None,
        *,
        state=None,
        emit_turn_start: bool = True,
        follow_up_idle_secs: float = 0.0,
        bare_wake_idle_secs: float = 0.0,
        wake_ack_wait_secs: float = 0.0,
        turn_observer=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._text_parts: list[str] = []
        self._on_turn = on_turn
        self._state = state
        # turn/start is a promise that a reply will eventually be voiced and a
        # tts/stop·abort will release the device's idle pause. Only the full
        # chain (with a TTS bridge) can honor it — the no-TTS (M2) chain would
        # dispatch a turn nobody answers, leaving the device disarmed.
        self._emit_turn_start = emit_turn_start
        # Idle windows the device should use after a reply: the follow-up window
        # of a normal conversation (W2), and the much shorter one after a
        # bare-wake ack (W1). 0 leaves the UserIdleController's own timeout
        # alone (tests / chains that manage it elsewhere).
        self._follow_up_idle_secs = follow_up_idle_secs
        self._bare_wake_idle_secs = bare_wake_idle_secs
        # The window the turn in flight should leave behind, installed when its
        # reply starts speaking (see _install_idle_window). Every dispatch
        # overwrites it. A turn whose reply never voices leaves it pending, and
        # the next bot-start to arrive claims it — normally the next turn's own,
        # but an injected line (tts_bridge.inject_text) speaks without a
        # dispatch, so it inherits the pending value. That is how the window has
        # always behaved for injects: they never managed it.
        self._pending_idle_secs = 0.0
        # How long to wait after listen/detect for the user to speak before
        # answering the wake word itself. 0 disables the bare-wake ack; a
        # negative value (misconfigured env) must not become an instant ack.
        self._wake_ack_wait_secs = max(0.0, wake_ack_wait_secs)
        self._wake_ack_task: asyncio.Task | None = None
        # Opens the turn scope + TurnStorage for a bare-wake ack, which has no
        # user utterance and therefore never reaches the observer's turn-stop
        # path. None in chains without an observer (the M2 no-TTS chain).
        self._turn_observer = turn_observer
        # The ack's telemetry scope, opened in the ack timer's task — which is
        # not this processor's own task, so nothing flushes it implicitly; we
        # hold the object and flush it explicitly (_flush_bare_wake_scope).
        self._bare_wake_scope = None
        # Turn context captured from the FIRST transcript of the turn. The
        # observer (upstream) resets state.turn_type to "follow_up" on
        # UserStoppedSpeakingFrame, so by the time this processor merges, the
        # wake_word classification is already gone — capture it while the
        # transcripts are still flowing (mid-turn, turn_type is still set).
        self._captured_turn_type: str = "follow_up"
        self._captured_wake_word: str = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterimTranscriptionFrame):
            # Interim transcripts are a SEPARATE class from finals (they do not
            # subclass TranscriptionFrame), and they are the earliest evidence
            # the user is mid-sentence — a wake ack must not answer over them.
            self._cancel_wake_ack()
        elif isinstance(frame, TranscriptionFrame):
            # The user spoke, so this is not a bare wake word any more — a wake
            # ack armed by listen/detect must not answer over them.
            self._cancel_wake_ack()
            if not self._text_parts and self._state is not None:
                self._captured_turn_type = self._state.turn_type
                self._captured_wake_word = self._state.wake_word or ""
            self._text_parts.append(frame.text)
        elif isinstance(frame, UserStartedSpeakingFrame):
            # Third net: some STT paths gate interims and announce the turn
            # before any transcript frame arrives.
            self._cancel_wake_ack()
        elif isinstance(frame, BotStartedSpeakingFrame):
            # The reply is voiced — install this turn's follow-up window. The
            # frame is forwarded FIRST: the controller has to see the bot-start
            # (which cancels its running follow-up timer) before the update, or
            # the update would restart that timer at the new value first.
            await self.push_frame(frame, direction)
            await self._install_idle_window()
            return
        elif isinstance(frame, UserStoppedSpeakingFrame):
            merged = " ".join(p for p in self._text_parts if p).strip()
            self._text_parts = []
            self._cancel_wake_ack()
            if merged:
                await self._dispatch_turn(self._wake_turn_text(merged), bare_wake=False)
        elif isinstance(frame, PiguguMessageFrame):
            self._on_control(frame.message)
        # Pass everything downstream (audio dies at the output transport, which
        # only sends Opus / control frames).
        await self.push_frame(frame, direction)

    def _on_control(self, message: dict) -> None:
        """listen/detect starts a wake turn: give the user a moment to speak on.

        If they don't, ``_wake_ack_later`` answers the wake word itself, which
        is what the persona reply for a bare wake is.
        """
        if message.get("type") != "listen":
            return
        if message.get("state") == "detect":
            self._arm_wake_ack(str(message.get("text", "") or ""))
        elif message.get("state") == "stop":
            # Device stopped listening — there is nothing left to answer.
            self._cancel_wake_ack()

    def _arm_wake_ack(self, wake_word: str) -> None:
        self._cancel_wake_ack()
        # A wake ack is a turn, so it needs the full chain (see _emit_turn_start).
        if not self._wake_ack_wait_secs or not self._emit_turn_start:
            return
        # Deliberately a bare asyncio task rather than self.create_task: the
        # processor's TaskManager only exists once it runs inside a pipeline.
        # The task is cancellable only until the timer fires — _wake_ack_later
        # drops the handle the moment it commits to dispatching, so a signal
        # arriving mid-dispatch cannot cancel the turn half-pushed (see there).
        self._wake_ack_task = asyncio.create_task(self._wake_ack_later(wake_word))

    async def _wake_ack_later(self, wake_word: str) -> None:
        try:
            await asyncio.sleep(self._wake_ack_wait_secs)
        except asyncio.CancelledError:
            return
        # Past this line the ack is committed and no longer cancellable: the
        # dispatch below pushes several frames in sequence, and cancelling it
        # part-way would leave the device with an stt/turn-start and a reply that
        # never comes (its idle pause stays armed). A user who starts speaking
        # within a few ms of the timer firing therefore gets BOTH turns — the
        # ack's reply is the one that gets barge-in'd, so the user still hears
        # the answer they asked for.
        self._wake_ack_task = None
        # The wake turn is over the moment the timer fires, answered or not.
        # Nothing else resets the classification for a bare wake (no
        # UserStoppedSpeakingFrame ever arrives), so without this the user's NEXT
        # utterance would keep turn_type="wake_word" — getting a fabricated
        # wake-word prefix, and being counted as a bare wake by the metrics.
        if self._state is not None:
            self._state.turn_type = "follow_up"
            self._state.wake_word = ""
        text = wake_turn_text("", wake_word)
        if not text:
            # Firmware sent listen/detect without a usable wake word (e.g. the
            # WakeNet model index mapped to nothing) — the bare wake gets no
            # answer. Say so rather than failing silently.
            logger.warning("[PiguguAgentGateway] bare wake has no wake word — not answering")
            return
        logger.info(f"[PiguguAgentGateway] BARE WAKE: '{text}'")
        try:
            await self._dispatch_turn(text, bare_wake=True)
        except Exception:
            logger.exception("[PiguguAgentGateway] bare-wake dispatch failed")

    def _cancel_wake_ack(self) -> None:
        if self._wake_ack_task is not None:
            self._wake_ack_task.cancel()
            self._wake_ack_task = None

    async def _dispatch_turn(self, text: str, *, bare_wake: bool) -> None:
        """Hand one user turn to the agent: idle window → stt → turn/start → turn."""
        if bare_wake and self._turn_observer is not None:
            # The ack is a turn of its own, but nothing in the audio path opens
            # one for it (no utterance, no stop carrying text) — open it here,
            # before the turn frame, so the TTS bridge finds a storage to fill
            # and the row is committed with the ack's text. A previous ack's
            # scope is finished by now, so flush it before opening the next.
            self._flush_bare_wake_scope()
            self._bare_wake_scope = self._turn_observer.begin_bare_wake_turn()
        self._pending_idle_secs = self._idle_secs(bare_wake)
        logger.info(f"[PiguguAgentGateway] TURN: '{text}'")
        if self._on_turn:
            await self._on_turn(text)
        # stt message first (device shows the user's text), then hand the turn
        # to the agent — parity with old connection.py:939.
        await self.push_frame(PiguguOutputMessageFrame(message={"type": "stt", "text": text}))
        # turn/start: generation for this turn is starting (before any audio is
        # ready). The device pauses its silence-idle timer so a slow LLM/tool
        # turn is never killed, and resets its follow-up window — the turn's
        # tts/stop·abort releases it. Every real turn (wake-word or follow-up)
        # passes through this dispatch.
        if self._emit_turn_start:
            await self.push_frame(
                PiguguOutputMessageFrame(message={"type": "turn", "state": "start"})
            )
        await self.push_frame(PiguguUserTurnFrame(text=text))

    async def _install_idle_window(self) -> None:
        """Tell the turn layer how long to wait for the user's next turn.

        A normal conversation keeps the generous follow-up window (W2); after a
        bare-wake ack the user has just been answered, so a much shorter window
        applies (W1). A window of 0 leaves the controller's own timeout alone.

        Installed when the reply STARTS speaking, not when it is dispatched:
        the controller applies an update immediately and, while it is waiting
        for the user, restarts the running timer with the new duration. Since a
        bare wake produces no user-speech signal, that timer is always running
        by the time the ack is dispatched — installing W1 there would put the
        ack's own generation time on a 5s clock, closing the session before the
        user hears anything. At bot-start the controller is not waiting for the
        user, so this only sets the value; the timer is armed from it at
        bot-stop, which is where the follow-up window begins.
        """
        secs, self._pending_idle_secs = self._pending_idle_secs, 0.0
        if secs <= 0:
            return
        # UPSTREAM: the UserIdleController lives in UserTurnProcessor, which
        # sits before this processor in the chain.
        await self.push_frame(UserIdleTimeoutUpdateFrame(timeout=secs), FrameDirection.UPSTREAM)

    def _idle_secs(self, bare_wake: bool) -> float:
        """This turn's follow-up window, or 0 to leave the timeout alone.

        A follow-up window of 0 means "do not manage the idle window at all"
        (VOICE_IDLE_SILENCE_SECS=0 is how idle close is disabled). Applying W1
        in that configuration would leave the short window stuck on every later
        turn, since nothing would ever overwrite it.
        """
        if self._follow_up_idle_secs <= 0:
            return 0.0
        secs = self._bare_wake_idle_secs if bare_wake else self._follow_up_idle_secs
        return secs if secs > 0 else 0.0

    def _flush_bare_wake_scope(self) -> None:
        """Hand the ack's telemetry scope to the exporter.

        The observer opens a normal turn's scope in its own task, so flushing it
        lazily is the observer's job. The ack's is opened by `begin_bare_wake_turn`
        in the ack timer's task, whose contextvar goes away with that task —
        without an explicit flush its latency row would never be enqueued.
        """
        scope, self._bare_wake_scope = self._bare_wake_scope, None
        if scope is not None:
            from metrics import registry

            registry.flush(scope)

    async def cleanup(self) -> None:
        self._cancel_wake_ack()
        self._flush_bare_wake_scope()
        await super().cleanup()

    def _wake_turn_text(self, text: str) -> str:
        """Build the wake turn's user text: the wake word, then what was said.

        The firmware does NOT stream the wake-word audio (SEND_WAKE_WORD_DATA is
        off), so the transcript never contains the wake word — it is put back
        here, making the wake turn read as the plain utterance it was: a bare
        "hey pigugu" when the user stopped at the wake word, "hey pigugu how are
        you" when they spoke on. The LLM then answers a bare wake word the way
        the persona answers being summoned, which is what the persona reply for
        a bare wake is.

        Applied to the merged text, so multiple is_final chunks cannot
        duplicate the prefix.
        """
        if self._captured_turn_type != "wake_word":
            return text
        return wake_turn_text(text, self._captured_wake_word)
