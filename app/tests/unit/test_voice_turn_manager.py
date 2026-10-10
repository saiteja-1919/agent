"""Tests for the turn manager: endpointing, barge-in and the opener guard (10.2).

Every timestamp is supplied, so these tests assert the exact moment a turn ends
rather than sleeping and hoping.
"""

from __future__ import annotations

import pytest

from app.voice.turn_manager import (
    Signal,
    TurnManager,
    TurnPhase,
)


def manager(**kwargs) -> TurnManager:
    defaults = dict(
        endpoint_silence_secs=1.0,
        max_turn_secs=20.0,
        min_speech_secs=0.25,
        listening_window_secs=5.0,
    )
    defaults.update(kwargs)
    return TurnManager(**defaults)


def speak(mgr: TurnManager, start: float, text: str, *, final_at: float, end_at: float = None):
    """Drive one parent utterance: start, partial, final, stop."""
    mgr.on_parent_speech_start(start)
    mgr.on_parent_partial(text[: max(1, len(text) // 2)], start + 0.2)
    mgr.on_stt_final(text, final_at)
    mgr.on_parent_speech_end(final_at if end_at is None else end_at)


def test_a_turn_ends_only_after_the_endpoint_silence_elapses():
    mgr = manager()
    speak(mgr, 0.0, "నేను రేపు కడతాను", final_at=0.8)

    early = mgr.tick(1.2)  # 0.4s after the last speech
    assert early.signal is Signal.NONE

    done = mgr.tick(2.0)  # exactly the endpoint silence
    assert done.signal is Signal.TURN_COMPLETE
    assert done.text == "నేను రేపు కడతాను"
    assert mgr.phase is TurnPhase.LISTENING


def test_a_turn_is_not_completable_before_any_speech():
    mgr = manager()
    mgr.begin_listening(0.0)
    assert mgr.tick(2.0).signal is Signal.NONE
    event = mgr.tick(6.0)
    assert event.signal is Signal.SILENCE_TIMEOUT
    assert event.waited_secs == pytest.approx(6.0)


def test_a_cough_is_a_silence_not_a_turn():
    mgr = manager()
    mgr.on_parent_speech_start(0.0)
    mgr.on_stt_final("hmm", 0.1)
    mgr.on_parent_speech_end(0.1)
    event = mgr.tick(1.5)
    assert event.signal is Signal.SILENCE_TIMEOUT
    assert event.text == ""


def test_a_long_turn_is_truncated_at_the_ceiling():
    mgr = manager(max_turn_secs=5.0)
    mgr.on_parent_speech_start(0.0)
    for moment in (1.0, 2.0, 3.0, 4.0, 4.6):
        mgr.on_parent_partial("one two three four five six", moment)
    assert mgr.tick(4.8).signal is Signal.NONE  # still talking, under the ceiling
    event = mgr.tick(5.5)
    assert event.signal is Signal.MAX_TURN_REACHED
    assert event.text == "one two three four five six"


def test_the_endpoint_timer_restarts_while_the_parent_keeps_talking():
    mgr = manager()
    mgr.on_parent_speech_start(0.0)
    mgr.on_parent_partial("first half", 0.5)
    mgr.on_parent_partial("first half second half", 3.0)
    assert mgr.tick(3.5).signal is Signal.NONE  # only half the silence so far
    done = mgr.tick(4.0)
    assert done.signal is Signal.TURN_COMPLETE
    assert done.text == "first half second half"


def test_the_opening_is_not_interruptible_and_the_words_are_buffered():
    mgr = manager(interruptible=False)
    mgr.on_veda_started_speaking(0.0)

    event = mgr.on_parent_speech_start(0.5)
    assert event.signal is Signal.OPENER_PROTECTED
    assert mgr.phase is TurnPhase.VEDA_SPEAKING
    assert mgr.protected_interruptions == 1

    # The opener finishes; now barge-in is allowed and buffered speech counts.
    mgr.on_veda_stopped_speaking(2.0)
    assert mgr.phase is TurnPhase.LISTENING
    mgr.allow_interruptions()

    mgr.on_veda_started_speaking(3.0)
    barge = mgr.on_parent_speech_start(3.4)
    assert barge.signal is Signal.INTERRUPT_VEDA
    assert mgr.phase is TurnPhase.PARENT_SPEAKING
    assert mgr.barge_ins == 1


def test_stopping_veda_after_a_barge_in_keeps_the_buffered_words():
    mgr = manager(interruptible=True)
    mgr.on_veda_started_speaking(0.0)
    mgr.on_parent_speech_start(0.5)
    mgr.on_parent_partial("అవును సర్", 0.8)

    mgr.on_veda_stopped_speaking(0.9)
    assert mgr.phase is TurnPhase.PARENT_SPEAKING
    assert mgr.parent_text == "అవును సర్"


def test_a_final_transcript_does_not_end_the_turn_by_itself():
    """A parent who pauses mid-sentence must not be cut off at the pause."""
    mgr = manager()
    mgr.on_parent_speech_start(0.0)
    mgr.on_stt_final("నేను", 0.5)
    assert mgr.tick(1.2).signal is Signal.NONE
    assert mgr.phase is TurnPhase.PARENT_SPEAKING


def test_partial_text_is_kept_so_a_turn_never_ends_empty():
    mgr = manager()
    mgr.on_parent_speech_start(0.0)
    mgr.on_parent_partial("evening after six", 0.4)
    mgr.on_parent_speech_end(0.5)
    event = mgr.tick(2.0)
    assert event.signal is Signal.TURN_COMPLETE
    assert event.text == "evening after six"


def test_ending_twice_stays_ended():
    mgr = manager()
    mgr.end()
    assert mgr.ended
    assert mgr.tick(99.0).signal is Signal.NONE
    assert mgr.on_parent_speech_start(99.0).signal is Signal.NONE
    assert mgr.phase is TurnPhase.ENDED


def test_begin_listening_clears_the_previous_turn():
    mgr = manager()
    speak(mgr, 0.0, "first", final_at=0.5)
    mgr.tick(2.0)
    mgr.begin_listening(3.0)
    assert mgr.parent_text == ""
    assert mgr.turn_started_at is None


def test_nonsense_timings_are_rejected_at_construction():
    with pytest.raises(ValueError):
        TurnManager(endpoint_silence_secs=0)
    with pytest.raises(ValueError):
        TurnManager(endpoint_silence_secs=10.0, max_turn_secs=5.0)
