import re
from pathlib import Path

from amzvid.config import LONG, SHORT, RenderConfig
from amzvid.models import Word
from amzvid.render.captions import ass_time, build_ass, escape_text
from amzvid.render.compose import choose_fit
from amzvid.render.kenburns import frame_count, pick_moves, zoompan_filter
from amzvid.render.scenes import choose_moves, frame_aligned_duration


def test_frame_alignment_rounds_up_to_whole_frames():
    frames, duration = frame_aligned_duration(4.01, 0.0, LONG)
    assert frames == 121
    assert abs(duration - frames / LONG.fps) < 1e-9


def test_frame_aligned_duration_covers_audio_plus_gap():
    frames, duration = frame_aligned_duration(4.00, 0.22, LONG)
    assert duration >= 4.22


def test_frame_aligned_duration_has_a_floor():
    frames, _ = frame_aligned_duration(0.0, 0.0, LONG)
    assert frames >= 2


def test_zoompan_has_no_accumulating_expression():
    f = zoompan_filter("zoom_in", 5.0, LONG)
    assert "zoom+" not in f and "pzoom" not in f
    assert f"s={LONG.width}x{LONG.height}" in f
    assert "d=1" in f


def test_zoompan_pan_stays_inside_the_plate():
    f = zoompan_filter("pan_right", 4.0, SHORT)
    assert "iw-iw/" in f and "ih-ih/" in f


def test_every_move_builds():
    for move in pick_moves(8):
        assert zoompan_filter(move, 3.0, LONG).startswith("zoompan=")


def test_unknown_move_raises():
    try:
        zoompan_filter("barrel_roll", 3.0, LONG)  # type: ignore[arg-type]
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_frame_count_never_divides_by_zero():
    assert frame_count(0.0, LONG) >= 2


def test_adjacent_moves_differ():
    moves = choose_moves(8, ["hook", "feature", "feature", "spec", "con", "feature", "verdict", "cta"])
    assert all(a != b for a, b in zip(moves, moves[1:]))


def test_hook_and_verdict_get_fixed_moves():
    moves = choose_moves(3, ["hook", "feature", "verdict"])
    assert moves[0] == "zoom_in"
    assert moves[2] == "zoom_out"


def test_square_image_on_wide_canvas_gets_blur_pad():
    assert choose_fit(1.0, LONG) == "blurpad"


def test_matching_aspect_gets_cover():
    assert choose_fit(16 / 9, LONG) == "cover"
    assert choose_fit(9 / 16, SHORT) == "cover"


def test_ass_time_format():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(61.5) == "0:01:01.50"
    assert ass_time(3661.25) == "1:01:01.25"
    assert ass_time(-5) == "0:00:00.00"


def test_escape_text_neutralizes_override_braces():
    assert "{" not in escape_text("a {b} c")


def test_ass_has_one_event_per_word_and_monotonic_times():
    words = [Word("alpha", 0.0, 0.4), Word("beta", 0.4, 0.8), Word("gamma", 0.8, 1.2)]
    text = build_ass(words, LONG, RenderConfig())
    events = [l for l in text.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 3
    times = [re.match(r"Dialogue: 0,([\d:.]+),([\d:.]+),", e).groups() for e in events]
    assert times == sorted(times)


def test_ass_highlights_exactly_one_word_per_event():
    cfg = RenderConfig()
    words = [Word("alpha", 0.0, 0.4), Word("beta", 0.4, 0.8)]
    for line in build_ass(words, LONG, cfg).splitlines():
        if line.startswith("Dialogue:"):
            assert line.count(f"\\c{cfg.highlight_color}") == 1


def test_ass_declares_the_canvas_resolution():
    text = build_ass([Word("x", 0, 0.3)], SHORT, RenderConfig())
    assert f"PlayResX: {SHORT.width}" in text
    assert f"PlayResY: {SHORT.height}" in text


def test_blank_path_settings_fall_back_to_defaults(tmp_path, monkeypatch):
    """A blank `assets_dir` must mean "use the default", not the current directory."""
    from amzvid.config import load_config

    config = tmp_path / "c.toml"
    config.write_text(f"""
[general]
affiliate_tag = "t-20"
work_dir   = "{tmp_path / 'w'}"
output_dir = ""
assets_dir = "   "

[voice]
voice_id = "v"
""")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    cfg = load_config(config)
    assert cfg.paths.assets.name == "assets"
    assert (cfg.paths.assets / "fonts").is_dir()
    assert cfg.paths.output != Path(".")
    assert cfg.paths.output.is_absolute()
