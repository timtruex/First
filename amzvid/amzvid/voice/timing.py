"""Turn ElevenLabs character alignment into word timings and caption chunks.

The alignment arrays are parallel: characters[i] spans
character_start_times_seconds[i] .. character_end_times_seconds[i]. Words are maximal runs
of non-whitespace characters, taking their start from the first character and their end
from the last. Word-accurate timing is what makes captions land on the syllable instead
of drifting a block at a time.
"""
from __future__ import annotations

from ..models import Word

LEADING_PUNCT = "\"'([{¿¡“‘"


def words_from_alignment(
    characters: list[str],
    starts: list[float],
    ends: list[float],
) -> list[Word]:
    if not (len(characters) == len(starts) == len(ends)):
        raise ValueError(
            f"alignment arrays disagree: {len(characters)}/{len(starts)}/{len(ends)}"
        )

    words: list[Word] = []
    buf: list[str] = []
    w_start = 0.0
    w_end = 0.0

    for char, start, end in zip(characters, starts, ends):
        if char.isspace():
            if buf:
                words.append(Word("".join(buf), w_start, max(w_end, w_start + 0.01)))
                buf = []
            continue
        if not buf:
            w_start = start
        buf.append(char)
        w_end = end

    if buf:
        words.append(Word("".join(buf), w_start, max(w_end, w_start + 0.01)))

    return _monotonic(words)


def _monotonic(words: list[Word]) -> list[Word]:
    """Clamp any backwards or zero-length spans so caption events never overlap."""
    fixed: list[Word] = []
    cursor = 0.0
    for word in words:
        start = max(word.start, cursor)
        end = max(word.end, start + 0.04)
        fixed.append(Word(word.text, start, end))
        cursor = end
    return fixed


def chunk_words(words: list[Word], per_chunk: int, max_gap: float = 0.6) -> list[list[Word]]:
    """Group words into caption lines.

    A line breaks on count, on sentence-ending punctuation, or on a pause longer than
    max_gap - a pause is where a viewer's eye expects the text to change.
    """
    if per_chunk < 1:
        raise ValueError("per_chunk must be >= 1")
    chunks: list[list[Word]] = []
    current: list[Word] = []

    for i, word in enumerate(words):
        current.append(word)
        ends_sentence = word.text.rstrip("\"')]}").endswith((".", "!", "?", ":"))
        long_pause = i + 1 < len(words) and (words[i + 1].start - word.end) > max_gap
        if len(current) >= per_chunk or ends_sentence or long_pause:
            chunks.append(current)
            current = []

    if current:
        if len(current) == 1 and chunks:
            chunks[-1].extend(current)  # a single orphan word reads badly on its own line
        else:
            chunks.append(current)
    return chunks


def shift(words: list[Word], dt: float) -> list[Word]:
    return [w.shifted(dt) for w in words]
