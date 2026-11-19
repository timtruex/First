"""Tests for the Agent SDK script backend.

A live call needs a signed-in Claude Code on the machine, so these cover the parts that
run locally: pulling a draft out of whatever shape the model replies in, and the guard
that stops this backend silently billing API credits instead of the plan.
"""
from __future__ import annotations

import json

import pytest

from amzvid.script.agent import AgentScriptError, _parse

DRAFT = {
    "scenes": [
        {"narration": "It fills a sixteen inch screen with Fire TV built in.",
         "kind": "hook", "on_screen_text": "Big Screen", "image_hint": "front view"},
        {"narration": "Mini LED with local dimming gives it real contrast.",
         "kind": "feature", "on_screen_text": "", "image_hint": "display"},
        {"narration": "Worth knowing: it needs mains power, so placement is fixed.",
         "kind": "con", "on_screen_text": "", "image_hint": ""},
        {"narration": "Good for a busy kitchen that already runs on Alexa.",
         "kind": "verdict", "on_screen_text": "Verdict", "image_hint": ""},
        {"narration": "The link is in the description.",
         "kind": "cta", "on_screen_text": "", "image_hint": ""},
    ],
    "pros": ["Bright display", "Fire TV built in", "Good speakers"],
    "cons": ["Needs mains power"],
    "verdict": "Buy it if your kitchen already runs on Alexa.",
    "youtube_title": "Echo Show 16 Review",
    "youtube_description": "A short look at the Echo Show 16.",
    "tags": ["echo show", "alexa", "smart display"],
    "shorts": [{"title": "Echo Show 16 in 30 seconds", "scene_indices": [0, 1]}],
    "thumbnail_text": "WORTH IT?",
}


def test_parses_a_bare_json_object():
    draft = _parse(json.dumps(DRAFT))
    assert len(draft.scenes) == 5
    assert draft.thumbnail_text == "WORTH IT?"


def test_parses_json_in_a_markdown_fence():
    draft = _parse("```json\n" + json.dumps(DRAFT) + "\n```")
    assert draft.youtube_title == "Echo Show 16 Review"


def test_parses_json_in_a_bare_fence():
    assert _parse("```\n" + json.dumps(DRAFT) + "\n```").pros[0] == "Bright display"


def test_parses_json_with_prose_around_it():
    """Models add a sentence even when told not to; that must not fail the job."""
    text = "Here is the script you asked for:\n\n" + json.dumps(DRAFT) + "\n\nHope that helps."
    assert len(_parse(text).scenes) == 5


def test_rejects_a_reply_with_no_json():
    with pytest.raises(AgentScriptError, match="no JSON object"):
        _parse("I am afraid I cannot write that script.")


def test_rejects_malformed_json():
    with pytest.raises(AgentScriptError, match="not valid JSON"):
        _parse('{"scenes": [}')


def test_rejects_json_of_the_wrong_shape():
    with pytest.raises(AgentScriptError, match="did not match"):
        _parse('{"something": "else"}')


def test_empty_reply_is_rejected():
    with pytest.raises(AgentScriptError):
        _parse("   ")


# --------------------------------------------------------------------------- #

def _cfg(tmp_path, monkeypatch, backend: str, api_key: str = ""):
    from amzvid.config import load_config

    body = f'\n[script]\nbackend = "{backend}"\n'
    if api_key:
        body += f'api_key = "{api_key}"\n'
    config = tmp_path / "c.toml"
    config.write_text(
        f'[general]\nwork_dir = "{tmp_path / "w"}"\noutput_dir = "{tmp_path / "o"}"\n'
        f'\n[voice]\nvoice_id = "v"\n{body}'
    )
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    return load_config(config)


def test_agent_backend_never_exports_an_api_key(tmp_path, monkeypatch):
    """Exporting one would route the run to credits while appearing to use the plan."""
    import os

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg(tmp_path, monkeypatch, "agent", api_key="sk-ant-should-not-be-used")
    assert cfg.script.backend == "agent"
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_api_backend_still_exports_the_key(tmp_path, monkeypatch):
    import os

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _cfg(tmp_path, monkeypatch, "api", api_key="sk-ant-from-file")
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-file"


def test_unknown_backend_is_rejected(tmp_path, monkeypatch):
    from amzvid.config import ConfigError

    with pytest.raises(ConfigError, match="backend"):
        _cfg(tmp_path, monkeypatch, "magic")


def test_agent_backend_reports_a_key_collision(tmp_path, monkeypatch):
    from amzvid.cli import _anthropic_credential

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leftover")
    cfg = _cfg(tmp_path, monkeypatch, "agent")
    _ok, note = _anthropic_credential(cfg)
    assert "WARNING" in note and "API credits" in note


def test_agent_backend_needs_no_key_when_none_is_set(tmp_path, monkeypatch):
    from amzvid.cli import _anthropic_credential

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, note = _anthropic_credential(_cfg(tmp_path, monkeypatch, "agent"))
    assert ok and "Claude plan" in note


# --------------------------------------------------------------------------- #

def _voice_config(tmp_path, monkeypatch, file_key: str | None, env_key: str | None):
    from amzvid.config import load_config

    body = f'api_key = "{file_key}"\n' if file_key else ""
    config = tmp_path / "v.toml"
    config.write_text(
        f'[general]\nwork_dir = "{tmp_path / "w"}"\noutput_dir = "{tmp_path / "o"}"\n'
        f'\n[voice]\nvoice_id = "v"\n{body}'
    )
    if env_key is None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ELEVENLABS_API_KEY", env_key)
    return load_config(config)


def test_voice_key_source_is_recorded(tmp_path, monkeypatch):
    """A leftover placeholder in the environment silently beat the real key in the file
    and produced a 401 that looked like a bad key. doctor must name the source."""
    cfg = _voice_config(tmp_path, monkeypatch, file_key="sk_real_key_value", env_key=None)
    assert cfg.voice.api_key == "sk_real_key_value"
    assert cfg.voice.api_key_source == "config.toml"


def test_environment_voice_key_wins_and_says_so(tmp_path, monkeypatch):
    cfg = _voice_config(
        tmp_path, monkeypatch, file_key="sk_real_key_value", env_key="unused-for-dry-run"
    )
    assert cfg.voice.api_key == "unused-for-dry-run"
    assert cfg.voice.api_key_source == "environment"


def test_key_description_flags_something_that_is_not_a_key():
    from amzvid.cli import _describe_key

    bad = _describe_key("unused-for-dry-run", "environment")
    assert "does not look like" in bad
    assert "unu" in bad and "18 chars" in bad
    assert "unused-for-dry-run" not in bad.replace("unu... 18 chars", "")

    good = _describe_key("sk_" + "a" * 40, "config.toml")
    assert "does not look like" not in good
    assert "config.toml" in good


def test_key_description_never_prints_the_whole_key():
    from amzvid.cli import _describe_key

    secret = "sk_" + "z" * 45
    assert secret not in _describe_key(secret, "config.toml")
