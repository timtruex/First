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


def _write_config(tmp_path, **overrides) -> Path:
    body = {
        "affiliate_tag": '"t-20"',
        "work_dir": f'"{tmp_path / "w"}"',
        "output_dir": f'"{tmp_path / "o"}"',
        "dry_run": "false",
    }
    body.update(overrides)
    config = tmp_path / "c.toml"
    config.write_text(
        "[general]\n"
        + "".join(f"{k} = {v}\n" for k, v in body.items())
        + '\n[voice]\nvoice_id = "PUT_YOUR_VOICE_ID_HERE"\n'
    )
    return config


def test_missing_voice_key_does_not_block_config_load(tmp_path, monkeypatch):
    """doctor and dry runs must work before any ElevenLabs credential exists."""
    from amzvid.config import load_config

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    cfg = load_config(_write_config(tmp_path))
    assert cfg.voice.api_key == ""


def test_placeholder_voice_id_reads_as_unset(tmp_path, monkeypatch):
    from amzvid.config import load_config

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert load_config(_write_config(tmp_path)).voice.voice_id == ""


def test_env_key_beats_config_file(tmp_path, monkeypatch):
    from amzvid.config import load_config

    monkeypatch.setenv("ELEVENLABS_API_KEY", "from-env")
    config = _write_config(tmp_path)
    config.write_text(config.read_text() + 'api_key = "from-file"\n')
    assert load_config(config).voice.api_key == "from-env"


def test_synthesis_refuses_without_a_key(tmp_path, monkeypatch):
    """The credential check moved to the point of use; it must still fire there."""
    from amzvid.config import load_config
    from amzvid.voice.elevenlabs import VoiceError, synthesize_scenes

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    cfg = load_config(_write_config(tmp_path))
    try:
        synthesize_scenes(cfg, ["hello"], tmp_path / "voice")
    except VoiceError as exc:
        assert "ELEVENLABS_API_KEY" in str(exc)
        return
    raise AssertionError("expected VoiceError when no API key is configured")


def test_synthesis_refuses_without_a_voice_id(tmp_path, monkeypatch):
    from amzvid.config import load_config
    from amzvid.voice.elevenlabs import VoiceError, synthesize_scenes

    monkeypatch.setenv("ELEVENLABS_API_KEY", "present")
    cfg = load_config(_write_config(tmp_path))
    try:
        synthesize_scenes(cfg, ["hello"], tmp_path / "voice")
    except VoiceError as exc:
        assert "voice_id" in str(exc)
        return
    raise AssertionError("expected VoiceError when no voice id is configured")


def _caption_cfg(mode: str):
    from amzvid.config import Config, Paths, RenderConfig, VoiceConfig

    return Config(
        paths=Paths(Path("/tmp"), Path("/tmp"), Path("/tmp/x"), Path("/tmp")),
        voice=VoiceConfig(api_key="k", voice_id="v"),
        telegram=None,
        render=RenderConfig(captions=mode),
    )


def test_caption_modes_select_the_right_canvases():
    from amzvid.pipeline import wants_captions

    expected = {
        "all": (True, True),
        "none": (False, False),
        "shorts": (False, True),   # the setting is "shorts"; the canvas is "short"
        "long": (True, False),
    }
    for mode, (on_long, on_short) in expected.items():
        cfg = _caption_cfg(mode)
        assert wants_captions(cfg, "long") is on_long, mode
        assert wants_captions(cfg, "short") is on_short, mode


def test_unknown_caption_mode_is_rejected_at_load(tmp_path, monkeypatch):
    from amzvid.config import ConfigError, load_config

    config = tmp_path / "c.toml"
    config.write_text(
        f'[general]\nwork_dir = "{tmp_path / "w"}"\noutput_dir = "{tmp_path / "o"}"\n'
        f'\n[voice]\nvoice_id = "v"\n\n[render]\ncaptions = "sometimes"\n'
    )
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    try:
        load_config(config)
    except ConfigError as exc:
        assert "captions" in str(exc)
        return
    raise AssertionError("expected ConfigError for an unknown caption mode")


def test_caption_mode_is_case_insensitive(tmp_path, monkeypatch):
    from amzvid.config import load_config

    config = tmp_path / "c.toml"
    config.write_text(
        f'[general]\nwork_dir = "{tmp_path / "w"}"\noutput_dir = "{tmp_path / "o"}"\n'
        f'\n[voice]\nvoice_id = "v"\n\n[render]\ncaptions = "  NONE "\n'
    )
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    assert load_config(config).render.captions == "none"


def test_invalid_toml_becomes_a_config_error(tmp_path, monkeypatch):
    """A typo in config.toml must not surface as a tomllib traceback."""
    from amzvid.config import ConfigError, load_config

    config = tmp_path / "c.toml"
    config.write_text("this is not toml [[[\n")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    try:
        load_config(config)
    except ConfigError as exc:
        assert "not valid TOML" in str(exc)
        return
    raise AssertionError("expected ConfigError for malformed TOML")


def test_missing_config_names_the_path(tmp_path):
    from amzvid.config import ConfigError, load_config

    try:
        load_config(tmp_path / "nope.toml")
    except ConfigError as exc:
        assert "nope.toml" in str(exc)
        return
    raise AssertionError("expected ConfigError for a missing config")


def test_env_var_pasted_as_a_toml_key_is_named():
    """`ELEVENLABS_API_KEY = ...` in config.toml is a common slip; say what to write."""
    from dataclasses import fields

    from amzvid.config import TelegramConfig, VoiceConfig, _suggest

    voice = {f.name for f in fields(VoiceConfig)}
    telegram = {f.name for f in fields(TelegramConfig)}

    assert 'api_key = "..."' in _suggest("ELEVENLABS_API_KEY", voice)
    assert 'bot_token = "..."' in _suggest("TELEGRAM_BOT_TOKEN", telegram)


def test_near_miss_keys_get_a_suggestion():
    from dataclasses import fields

    from amzvid.config import VoiceConfig, _suggest

    assert "voice_id" in _suggest("voiceid", {f.name for f in fields(VoiceConfig)})


def test_unrelated_key_gets_no_invented_suggestion():
    from dataclasses import fields

    from amzvid.config import RenderConfig, _suggest

    assert _suggest("wibble", {f.name for f in fields(RenderConfig)}) == "wibble"


def test_unknown_key_error_names_the_section(tmp_path, monkeypatch):
    from amzvid.config import ConfigError, load_config

    config = tmp_path / "c.toml"
    config.write_text(
        f'[general]\nwork_dir = "{tmp_path / "w"}"\noutput_dir = "{tmp_path / "o"}"\n'
        f'\n[voice]\nvoice_id = "v"\nELEVENLABS_API_KEY = "x"\n'
    )
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    try:
        load_config(config)
    except ConfigError as exc:
        assert "[voice]" in str(exc) and "api_key" in str(exc)
        return
    raise AssertionError("expected ConfigError naming the section")


def test_script_prompt_is_positive_and_bullet_led():
    """The narration should showcase the product, not open on a problem the viewer has."""
    from amzvid.script.prompts import SYSTEM

    assert "Positive and enthusiastic" in SYSTEM
    assert "Never open on a complaint" in SYSTEM
    assert "listing bullets are your source material" in SYSTEM
    assert "specific problem" not in SYSTEM, "the problem-first hook rule is still there"


def test_drawback_scene_is_configurable():
    from pathlib import Path as _P

    from amzvid.config import Config, Paths, ScriptConfig, VoiceConfig
    from amzvid.models import Product, ProductImage
    from amzvid.script.prompts import build_user_prompt

    def cfg(include: bool) -> Config:
        return Config(
            paths=Paths(_P("/tmp"), _P("/tmp"), _P("/tmp/x"), _P("/tmp")),
            voice=VoiceConfig(api_key="k", voice_id="v"),
            telegram=None,
            script=ScriptConfig(include_drawback=include),
        )

    product = Product(
        asin="B0", title="Thing", affiliate_url="u", bullets=["Does a thing"],
        images=[ProductImage(path=_P("/tmp/a.jpg"))],
    )
    assert 'kind "con"' in build_user_prompt(cfg(True), product)
    assert "Do not include a drawback" in build_user_prompt(cfg(False), product)


def test_rating_reaches_the_prompt_only_when_supplied():
    from pathlib import Path as _P

    from amzvid.config import Config, Paths, VoiceConfig
    from amzvid.models import Product, ProductImage
    from amzvid.script.prompts import build_user_prompt

    base = Config(
        paths=Paths(_P("/tmp"), _P("/tmp"), _P("/tmp/x"), _P("/tmp")),
        voice=VoiceConfig(api_key="k", voice_id="v"),
        telegram=None,
    )
    img = [ProductImage(path=_P("/tmp/a.jpg"))]
    with_rating = Product(asin="B0", title="T", affiliate_url="u", rating="4.4",
                          review_count="1,284 ratings", images=img)
    without = Product(asin="B0", title="T", affiliate_url="u", images=img)

    assert "4.4" in build_user_prompt(base, with_rating)
    assert "Rating:" not in build_user_prompt(base, without)


def _key_config(tmp_path, body: str):
    config = tmp_path / "c.toml"
    config.write_text(
        f'[general]\nwork_dir = "{tmp_path / "w"}"\noutput_dir = "{tmp_path / "o"}"\n'
        f'\n[voice]\nvoice_id = "v"\n{body}'
    )
    return config


def test_anthropic_key_can_live_in_the_config(tmp_path, monkeypatch):
    """launchd does not read a shell profile, so an exported key fails as a service."""
    from amzvid.config import load_config

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    cfg = load_config(_key_config(tmp_path, '\n[script]\napi_key = "sk-ant-from-file"\n'))
    assert cfg.script.api_key == "sk-ant-from-file"
    # The SDK reads only the environment, so loading must put it there.
    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-file"


def test_environment_anthropic_key_wins_over_the_file(tmp_path, monkeypatch):
    from amzvid.config import load_config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    cfg = load_config(_key_config(tmp_path, '\n[script]\napi_key = "sk-ant-from-file"\n'))
    assert cfg.script.api_key == "sk-ant-from-env"


def test_no_anthropic_key_anywhere_is_not_fatal_at_load(tmp_path, monkeypatch):
    """doctor must still run and report it; only a real run needs the key."""
    from amzvid.config import load_config

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    assert load_config(_key_config(tmp_path, "")).script.api_key == ""
