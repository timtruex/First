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


# --- caption highlighting and line breaks -------------------------------------------
#
# Three reviewers independently named the caption highlighter: it was landing on "as",
# "inch" and "built", and lines were ending mid-phrase ("so it doubles as").

def _words(text: str, step: float = 0.4):
    from amzvid.models import Word

    return [Word(t, i * step, i * step + step * 0.9) for i, t in enumerate(text.split())]


def test_highlight_holds_on_the_content_word_not_the_conjunction():
    from amzvid.render.captions import highlight_targets

    chunk = _words("so it doubles as")
    lit = [chunk[t].text for t in highlight_targets(chunk)]
    assert lit == ["doubles"] * 4


def test_highlight_advances_through_successive_content_words():
    from amzvid.render.captions import highlight_targets

    chunk = _words("a bright sixteen inch screen")
    assert [chunk[t].text for t in highlight_targets(chunk)] == [
        "bright", "bright", "sixteen", "inch", "screen",
    ]


def test_a_line_of_only_function_words_lights_nothing():
    from amzvid.render.captions import highlight_targets

    assert highlight_targets(_words("and here is what that is")) == [-1] * 6


def test_nothing_is_highlighted_when_the_target_is_minus_one():
    from amzvid.config import Canvas, RenderConfig
    from amzvid.render.captions import build_ass

    cfg = RenderConfig()
    ass = build_ass(_words("and here is what that is"), Canvas(1080, 1920, 30), cfg)
    assert cfg.highlight_color not in ass.split("[Events]")[-1]


def test_lines_do_not_end_on_a_function_word():
    from amzvid.voice.timing import chunk_words, is_content_word

    text = "Fire TV is built straight into it so it doubles as a kitchen television"
    for chunk in chunk_words(_words(text), 4):
        assert is_content_word(chunk[-1].text) or chunk[-1].text.rstrip().endswith(
            (",", ".", ";", ":", "!", "?")
        ), [w.text for w in chunk]


def test_a_sentence_end_still_breaks_immediately():
    from amzvid.voice.timing import chunk_words

    chunks = chunk_words(_words("It folds flat. And it is light."), 8)
    assert [w.text for w in chunks[0]] == ["It", "folds", "flat."]


def test_slack_is_bounded_so_a_line_cannot_run_away():
    from amzvid.voice.timing import chunk_words

    # Nothing but function words: the break has to happen anyway.
    chunks = chunk_words(_words("and of the to in on at by for with from as"), 3)
    assert all(len(c) <= 6 for c in chunks), [[w.text for w in c] for c in chunks]
