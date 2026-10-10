"""Tests for the response chunker (section 14.2).

The invariant that matters is losslessness: whatever the splitter does, the
spoken reply must be exactly the reply that was validated by policy.
"""

from __future__ import annotations

import pytest

from app.voice.chunker import (
    PAUSE_AFTER_CLAUSE_SECS,
    PAUSE_AFTER_QUESTION_SECS,
    PAUSE_AFTER_SENTENCE_SECS,
    PAUSE_FINAL_SECS,
    chunk_reply,
    join_segments,
    normalise,
)


def assert_lossless(text: str, **kwargs) -> list:
    segments = chunk_reply(text, **kwargs)
    assert join_segments(segments) == normalise(text)
    return segments


def test_empty_text_produces_no_segments():
    assert chunk_reply("") == []
    assert chunk_reply("     ") == []


def test_one_sentence_is_one_segment():
    segments = assert_lossless("సర్, మీ child school fees five thousand rupees ఇంకా pending ఉంది సర్.")
    assert len(segments) == 1
    assert segments[0].is_final
    assert segments[0].pause_after_secs == PAUSE_FINAL_SECS


def test_a_question_is_marked_and_paused_longer():
    segments = assert_lossless(
        "సర్, మీ child school fees five thousand rupees ఇంకా pending ఉంది సర్. "
        "ఎప్పటికల్లా కడతారో తెలుసుకోవచ్చా?"
    )
    assert len(segments) == 2
    assert segments[0].is_question is False
    assert segments[1].is_question is True
    assert segments[1].is_final is True
    assert segments[0].pause_after_secs == PAUSE_AFTER_SENTENCE_SECS


def test_a_comma_gives_a_shorter_pause_than_a_full_stop():
    segments = chunk_reply("అర్థమైంది సర్, మీకు వీలైనంత త్వరగా కట్టండి సర్. Thank you, have a nice day.")
    pauses = [segment.pause_after_secs for segment in segments[:-1]]
    assert pauses, "there must be a pause to compare"
    assert all(pause in (PAUSE_AFTER_CLAUSE_SECS, PAUSE_AFTER_SENTENCE_SECS) for pause in pauses)
    assert PAUSE_AFTER_CLAUSE_SECS <= PAUSE_AFTER_SENTENCE_SECS


def test_a_long_sentence_is_split_without_losing_a_word():
    text = (
        "సర్, ఈ విషయం principal గారికి inform చేయమని office కి note చేస్తాను సర్. "
        "Principal గారు free అయ్యాక office నుండి మీకు inform చేయమంటారా?"
    )
    segments = assert_lossless(text, max_chars=40)
    assert len(segments) > 1
    assert all(segment.chars <= 40 for segment in segments)
    assert segments[-1].is_final


def test_a_word_is_never_split_across_segments():
    words = [" ".join(f"word{i}word{i}word{i}" for i in range(start, start + 8)) for start in range(0, 40, 8)]
    text = " ".join(words)
    segments = assert_lossless(text, max_words=5, max_chars=30)
    vocabulary = set(text.split())
    for segment in segments:
        for token in segment.text.split():
            assert token in vocabulary, f"the splitter broke {token!r}"


def test_an_abbreviation_does_not_end_a_sentence():
    segments = assert_lossless("Meet Mr. Rama tomorrow at the office please.")
    assert len(segments) == 1


def test_every_segment_carries_its_index_and_only_the_last_is_final():
    segments = assert_lossless("First sentence here. Second one too. Third and last.")
    assert [segment.index for segment in segments] == [0, 1, 2]
    assert [segment.is_final for segment in segments] == [False, False, True]


def test_double_punctuation_stays_with_its_sentence():
    segments = assert_lossless("Really? Yes it is. Fine!")
    assert join_segments(segments) == "Really? Yes it is. Fine!"


def test_whitespace_is_collapsed_but_text_is_preserved():
    segments = assert_lossless("  hello   there   friend  ")
    assert segments[0].text == "hello there friend"
