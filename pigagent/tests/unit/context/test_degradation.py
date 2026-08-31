from core.llm.types import Message
from roast.constants import FREE_CHAT_EVENT_PREFIX

from context.degradation import (
    DEGRADATION_CORRECTION,
    apply_degradation_guard,
    detect_degradation,
)


def _a(text):
    return Message(role="assistant", content=text)


def test_identical_replies_trigger():
    msgs = [_a("Say that again — I drifted off for a second.")] * 3
    assert detect_degradation(msgs) is True


def test_normalization_case_and_punctuation():
    msgs = [
        _a("Say that again — I drifted off for a second."),
        _a("say that again i drifted off for a second"),
        _a("Say that again, I drifted off for a second!"),
    ]
    assert detect_degradation(msgs) is True


def test_varied_replies_do_not_trigger():
    msgs = [_a("Good morning, folks."), _a("Tell me a joke, please."), _a("Politics is a disaster.")]
    assert detect_degradation(msgs) is False


def test_short_history_never_triggers():
    # min_consecutive=3 requires at least 3 assistant replies.
    assert detect_degradation([_a("hello")]) is False
    assert detect_degradation([_a("hello"), _a("hello")]) is False


def test_broken_run_does_not_trigger():
    # 3 replies where the middle one differs — not a repetition run.
    msgs = [_a("Say that again."), _a("A totally different answer."), _a("Say that again.")]
    assert detect_degradation(msgs) is False


def test_only_assistant_replies_count():
    msgs = [
        Message(role="system", content="[Session Start]"),
        Message(role="user", content="hello"),
        _a("Say that again."),
        _a("Say that again."),
        _a("Say that again."),
        Message(role="user", content="are you there?"),
    ]
    assert detect_degradation(msgs) is True


def test_min_consecutive_2():
    msgs = [_a("Say that again."), _a("Say that again.")]
    assert detect_degradation(msgs, min_consecutive=2) is True
    assert detect_degradation(msgs, min_consecutive=3) is False


def test_apply_guard_appends_correction_when_detected():
    msgs = [_a("Say that again.")] * 3
    before = len(msgs)
    fired = apply_degradation_guard(msgs)
    assert fired is True
    assert len(msgs) == before + 1
    assert msgs[-1].role == "system"
    assert msgs[-1].content.startswith(f"{FREE_CHAT_EVENT_PREFIX}\n")
    assert DEGRADATION_CORRECTION in msgs[-1].content


def test_apply_guard_noop_when_clean():
    msgs = [_a("Good morning."), _a("A joke, please."), _a("Politics is a mess.")]
    before = list(msgs)
    fired = apply_degradation_guard(msgs)
    assert fired is False
    assert msgs == before


def test_partial_assistant_messages_excluded():
    # An interrupted/partial reply must not count toward the repetition run.
    msgs = [
        Message(role="user", content="hi"),
        _a("Say that again — I drifted off for a second."),
        Message(role="user", content="are you there?"),
        Message(role="assistant", content="Say that again — I drifted off", partial=True),
        Message(role="user", content="hello?"),
        _a("Say that again — I drifted off for a second."),
    ]
    assert detect_degradation(msgs) is False
    # A third full identical reply completes the run — fires.
    msgs.append(Message(role="user", content="please respond"))
    msgs.append(_a("Say that again — I drifted off for a second."))
    assert detect_degradation(msgs) is True


def test_user_repeating_question_does_not_trigger():
    # User asks the same thing repeatedly; the assistant's matching answer is
    # legitimate — not a degeneration loop.
    msgs = []
    for _ in range(3):
        msgs.append(Message(role="user", content="What is your name?"))
        msgs.append(_a("I'm Pigugu."))
    assert detect_degradation(msgs) is False


def test_assistant_repeat_across_varied_user_inputs_triggers():
    # The real spiral: different user prompts, same assistant reply.
    msgs = [
        Message(role="user", content="Hello, are you there?"),
        _a("Say that again — I drifted off for a second."),
        Message(role="user", content="Can you hear me now?"),
        _a("Say that again — I drifted off for a second."),
        Message(role="user", content="Good evening, are you listening?"),
        _a("Say that again — I drifted off for a second."),
    ]
    assert detect_degradation(msgs) is True
