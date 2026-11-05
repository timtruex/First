from amzvid.models import Word
from amzvid.voice.timing import chunk_words, shift, words_from_alignment


def alignment(text: str, per_char: float = 0.05):
    chars = list(text)
    starts = [i * per_char for i in range(len(chars))]
    ends = [(i + 1) * per_char for i in range(len(chars))]
    return chars, starts, ends


def test_words_split_on_whitespace():
    words = words_from_alignment(*alignment("hi there you"))
    assert [w.text for w in words] == ["hi", "there", "you"]


def test_word_spans_first_to_last_character():
    words = words_from_alignment(*alignment("ab cd"))
    assert words[0].start == 0.0
    assert words[0].end == 0.10
    assert abs(words[1].start - 0.15) < 1e-9


def test_mismatched_arrays_raise():
    try:
        words_from_alignment(["a", "b"], [0.0], [0.1])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_timings_are_monotonic_even_when_input_is_not():
    words = words_from_alignment(["a", " ", "b"], [0.5, 0.6, 0.1], [0.6, 0.7, 0.2])
    assert words[1].start >= words[0].end


def test_leading_and_trailing_space_do_not_make_empty_words():
    words = words_from_alignment(*alignment("  hi  "))
    assert [w.text for w in words] == ["hi"]


def test_chunks_break_on_count():
    words = [Word(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(8)]
    chunks = chunk_words(words, per_chunk=4)
    assert [len(c) for c in chunks] == [4, 4]


def test_chunks_break_on_sentence_end():
    words = [Word("one", 0, 0.2), Word("two.", 0.2, 0.4), Word("three", 0.4, 0.6)]
    chunks = chunk_words(words, per_chunk=5)
    assert [len(c) for c in chunks] == [2, 1] or [len(c) for c in chunks] == [3]


def test_chunks_break_on_long_pause():
    words = [Word("a", 0, 0.2), Word("b", 2.0, 2.2), Word("c", 2.2, 2.4)]
    chunks = chunk_words(words, per_chunk=10)
    assert len(chunks) == 2


def test_orphan_word_joins_previous_chunk():
    words = [Word(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(5)]
    chunks = chunk_words(words, per_chunk=4)
    assert len(chunks) == 1
    assert len(chunks[0]) == 5


def test_shift_moves_every_word():
    words = [Word("a", 1.0, 1.5)]
    assert shift(words, 2.0)[0].start == 3.0
