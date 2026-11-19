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


#: Words that carry no meaning on their own. A caption line that ends on one leaves the
#: viewer mid-thought ("so it doubles as"), and a highlight that lands on one draws the
#: eye to nothing. Both are judged against this set.
FUNCTION_WORDS = frozenset("""
a an the and or but so as at by for from in into of off on onto out over to up with
is are was were be been being am it its it's this that these those there here
i you he she we they me him her us them my your his our their
do does did done have has had can could will would shall should may might must
if then than when while because about after before again just also very too not no
what which who whom whose how where why
all any some each every more most much many other such own same only even still
really quite rather actually basically literally well now get got go going
""".split())


def is_content_word(text: str) -> bool:
    """True when a word is worth ending a line on, or worth highlighting."""
    bare = text.strip(""" .,!?:;"'()[]{}-""").casefold()
    if not bare:
        return False
    if any(ch.isdigit() for ch in bare):
        return True
    return bare not in FUNCTION_WORDS


def chunk_words(words: list[Word], per_chunk: int, max_gap: float = 0.6) -> list[list[Word]]:
    """Group words into caption lines.

    A line breaks on count, on punctuation, or on a pause longer than max_gap - a pause
    is where a viewer's eye expects the text to change. Once the count is reached the
    break slides forward past any trailing function word, because a line that ends on
    "as" or "the" reads as a sentence that got cut off rather than one that paused.
    """
    if per_chunk < 1:
        raise ValueError("per_chunk must be >= 1")
    chunks: list[list[Word]] = []
    current: list[Word] = []
    slack = 0

    for i, word in enumerate(words):
        current.append(word)
        stripped = word.text.rstrip("\"')]}")
        ends_sentence = stripped.endswith((".", "!", "?", ":"))
        ends_clause = stripped.endswith((",", ";"))
        long_pause = i + 1 < len(words) and (words[i + 1].start - word.end) > max_gap
        over = len(current) >= per_chunk

        if ends_sentence or long_pause:
            chunks.append(current)
            current = []
            slack = 0
            continue
        if not over:
            continue
        # At the limit. Break here only if the line ends on something meaningful, or a
        # comma lets it land cleanly. Otherwise carry on a word at a time, up to three
        # extra, rather than cut the phrase open.
        if ends_clause or is_content_word(word.text) or slack >= 3:
            chunks.append(current)
            current = []
            slack = 0
        else:
            slack += 1

    if current:
        if len(current) == 1 and chunks:
            chunks[-1].extend(current)  # a single orphan word reads badly on its own line
        else:
            chunks.append(current)
    return chunks


def shift(words: list[Word], dt: float) -> list[Word]:
    return [w.shifted(dt) for w in words]
