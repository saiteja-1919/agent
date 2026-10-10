"""Response chunker: turn one reply into segments TTS can start on at once (14.2).

The reply is generated as text, but speech cannot wait for the whole thing.
Splitting at sentence boundaries lets the first audio reach the parent while the
rest is still being synthesised.

The rules that make a split safe:

* **Nothing is lost, added or reordered.** Joining every segment's text with a
  single space reproduces the reply exactly, so a chunk boundary can never drop
  or repeat a word.
* **A word is never split.** Overflow is broken at clause and then word
  boundaries, always between words.
* **Questions keep their mark.** A segment that asks something ends in ``?``,
  so the pause after it can be longer - the parent may answer immediately.
* **The pause follows the punctuation**, not a fixed count: a comma wants a
  breath, a full stop wants a beat, a question wants a moment of silence.

Segments are small enough for one streaming TTS call and large enough that the
speech does not sound stitched together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

#: Marks that end a sentence: Latin, Telugu/Devanagari danda, CJK full stop.
_SENTENCE_END = ".!?।।。！？"

#: Kept with the piece that precedes them, so a comma does not start a segment.
_CLAUSE_SEPARATORS = ",;:—–-"

#: Abbreviations that must not end a sentence even though they end with a dot.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "sr", "sri", "shri", "rs", "no", "st", "vs",
    "e.g", "i.e", "u.s.a", "pvt", "ltd", "govt", "prof", "capt", "col",
}

#: Pauses after each kind of ending, in seconds (section 14.2).
PAUSE_AFTER_QUESTION_SECS = 1.2
PAUSE_AFTER_SENTENCE_SECS = 0.45
PAUSE_AFTER_CLAUSE_SECS = 0.18
PAUSE_FINAL_SECS = 0.0

#: Defaults sized for one streaming TTS request.
DEFAULT_MAX_CHARS = 160
DEFAULT_MAX_WORDS = 24


@dataclass(frozen=True)
class Segment:
    """One piece of speech, ready for the synthesiser."""

    index: int
    text: str
    pause_after_secs: float
    is_final: bool
    is_question: bool

    @property
    def chars(self) -> int:
        return len(self.text)


def normalise(text: str) -> str:
    """Collapse whitespace. The only change made to a reply before speaking."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _ends_abbreviation(joined: str) -> bool:
    """True when the trailing dot belongs to "Mr." and friends, not a stop."""
    without_dot = joined.rstrip(".")
    if not without_dot:
        return False
    token = without_dot.split()[-1].lower()
    return token in _ABBREVIATIONS


def _split_sentences(text: str) -> List[str]:
    """Split on sentence marks, keeping the mark with the sentence it ends."""
    pieces: List[str] = []
    current: List[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        current.append(char)
        if char in _SENTENCE_END:
            while index + 1 < len(text) and text[index + 1] in _SENTENCE_END:
                index += 1
                current.append(text[index])
            if char == "." and _ends_abbreviation("".join(current)):
                index += 1
                continue
            while index + 1 < len(text) and text[index + 1].isspace():
                index += 1
                current.append(text[index])
            piece = "".join(current).strip()
            if piece:
                pieces.append(piece)
            current = []
        index += 1
    tail = "".join(current).strip()
    if tail:
        pieces.append(tail)
    return [piece for piece in pieces if piece]


def _split_clauses(text: str) -> List[str]:
    """Break an over-long sentence at clause marks, punctuation kept on the left."""
    pieces: List[str] = []
    current: List[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        current.append(char)
        if char in _CLAUSE_SEPARATORS:
            # A hyphen inside a word ("part-time") is not a clause break.
            if char in "-–—" and not (index + 1 < len(text) and text[index + 1].isspace()):
                index += 1
                continue
            while index + 1 < len(text) and text[index + 1].isspace():
                index += 1
                current.append(text[index])
            piece = "".join(current).strip()
            if piece:
                pieces.append(piece)
            current = []
        index += 1
    tail = "".join(current).strip()
    if tail:
        pieces.append(tail)
    return [piece for piece in pieces if piece]


def _split_words(text: str, max_words: int, max_chars: int) -> List[str]:
    """Last resort: pack words greedily, never breaking one in half."""
    words = text.split()
    if len(words) <= max_words and len(text) <= max_chars:
        return [text]
    parts: List[str] = []
    current: List[str] = []
    for word in words:
        joined = " ".join(current + [word])
        if current and (len(current) >= max_words or len(joined) > max_chars):
            parts.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        parts.append(" ".join(current))
    return parts


def _fits(piece: str, max_chars: int, max_words: int) -> bool:
    return len(piece) <= max_chars and len(piece.split()) <= max_words


def _refine(piece: str, max_chars: int, max_words: int) -> List[str]:
    """Split one piece until every part fits, preferentially at clause marks."""
    if _fits(piece, max_chars, max_words):
        return [piece]
    clause_parts = _split_clauses(piece)
    if len(clause_parts) > 1:
        out: List[str] = []
        for part in clause_parts:
            out.extend(_refine(part, max_chars, max_words))
        return out
    return [part for part in _split_words(piece, max_words, max_chars) if part]


def _pause_for(piece: str) -> float:
    stripped = piece.rstrip()
    if stripped.endswith("?") or stripped.endswith("？"):
        return PAUSE_AFTER_QUESTION_SECS
    if stripped and stripped[-1] in _SENTENCE_END:
        return PAUSE_AFTER_SENTENCE_SECS
    return PAUSE_AFTER_CLAUSE_SECS


def chunk_reply(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_words: int = DEFAULT_MAX_WORDS,
) -> List[Segment]:
    """Split ``text`` into speakable segments. Empty input yields no segments.

    The invariant worth asserting: ``join_segments(chunk_reply(text))`` equals
    ``normalise(text)``.
    """
    cleaned = normalise(text)
    if not cleaned:
        return []

    pieces: List[str] = []
    for sentence in _split_sentences(cleaned):
        pieces.extend(_refine(sentence, max_chars, max_words))
    pieces = [normalise(piece) for piece in pieces]
    pieces = [piece for piece in pieces if piece]

    segments: List[Segment] = []
    total = len(pieces)
    for index, piece in enumerate(pieces):
        is_final = index == total - 1
        segments.append(
            Segment(
                index=index,
                text=piece,
                pause_after_secs=PAUSE_FINAL_SECS if is_final else _pause_for(piece),
                is_final=is_final,
                is_question=piece.rstrip().endswith("?"),
            )
        )
    return segments


def join_segments(segments: List[Segment]) -> str:
    """Rebuild the spoken reply. Used by tests to prove nothing was lost."""
    return " ".join(segment.text for segment in segments)
