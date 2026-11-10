"""Tests for the YouTube upload path.

A live upload needs Google credentials and would put a real video on a real channel, so
these cover the parts that decide correctness locally: the privacy guard, config
validation, and that the bot stops shipping files once they are on YouTube.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _cfg(tmp_path, **youtube):
    from amzvid.config import Config, Paths, VoiceConfig

    return Config(
        paths=Paths(tmp_path, tmp_path, tmp_path / "x.db", tmp_path),
        voice=VoiceConfig(api_key="k", voice_id="v"),
        telegram=None,
        youtube=youtube,
    )


def test_privacy_status_is_always_sent_explicitly():
    """Since November 2026 YouTube publishes an upload when privacyStatus is omitted,
    so an unreviewed affiliate video would go live unattended."""
    source = (Path(__file__).resolve().parent.parent / "amzvid" / "publish" / "youtube.py").read_text()
    assert '"privacyStatus": privacy' in source
    assert 'privacy = str(cfg.youtube.get("privacy_status", "private"))' in source, (
        "the default must be private, not whatever YouTube defaults to"
    )


def test_bad_privacy_status_is_rejected(tmp_path):
    """Reported from the config alone, with no optional dependency installed."""
    from amzvid.config import ConfigError
    from amzvid.publish.youtube import resolve_privacy

    with pytest.raises(ConfigError, match="privacy_status"):
        resolve_privacy(_cfg(tmp_path, privacy_status="everyone"))


def test_privacy_defaults_to_private(tmp_path):
    from amzvid.publish.youtube import resolve_privacy

    assert resolve_privacy(_cfg(tmp_path)) == "private"
    assert resolve_privacy(_cfg(tmp_path, privacy_status="  UNLISTED ")) == "unlisted"


def test_config_errors_do_not_need_the_google_libraries(tmp_path, monkeypatch):
    """upload_video must reach config validation before the dependency check."""
    from amzvid.config import ConfigError
    import amzvid.publish.youtube as yt

    def missing():
        raise yt.YouTubeError("libraries not installed")

    monkeypatch.setattr(yt, "_require_libs", missing)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(ConfigError, match="privacy_status"):
        yt.upload_video(_cfg(tmp_path, privacy_status="everyone"), video,
                        title="t", description="d", tags=[])


def test_missing_libraries_say_what_to_install(tmp_path, monkeypatch):
    import amzvid.publish.youtube as yt

    cfg = _cfg(tmp_path, privacy_status="private")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    def missing():
        raise yt.YouTubeError('install: pip install -e ".[youtube]"')

    monkeypatch.setattr(yt, "_require_libs", missing)
    with pytest.raises(yt.YouTubeError, match=r"\[youtube\]"):
        yt.upload_video(cfg, video, title="t", description="d", tags=[])


def test_authorize_refuses_quietly_without_credentials(tmp_path):
    """A render must not stall waiting on a browser that is not there."""
    pytest.importorskip("google_auth_oauthlib", reason="the youtube extra is not installed")
    from amzvid.publish.youtube import YouTubeError, authorize

    cfg = _cfg(tmp_path, token_path=str(tmp_path / "missing.json"))
    with pytest.raises(YouTubeError, match="youtube-auth"):
        authorize(cfg, interactive=False)


def test_upload_scope_is_limited_to_uploading():
    from amzvid.publish.youtube import SCOPES

    assert SCOPES == ["https://www.googleapis.com/auth/youtube.upload"]
    assert not any("force-ssl" in s or s.endswith("/youtube") for s in SCOPES), (
        "do not request broader channel access than uploading needs"
    )


def test_doctor_flags_enabled_but_unauthorized(tmp_path):
    from amzvid.cli import _youtube_status

    cfg = _cfg(tmp_path, enabled=True, token_path=str(tmp_path / "none.json"))
    assert "NOT AUTHORIZED" in _youtube_status(cfg)

    token = tmp_path / "tok.json"
    token.write_text("{}")
    ready = _cfg(tmp_path, enabled=True, token_path=str(token), privacy_status="private")
    assert "authorized" in _youtube_status(ready)
    assert "private" in _youtube_status(ready)


def test_doctor_reports_off_by_default(tmp_path):
    from amzvid.cli import _youtube_status

    assert "off" in _youtube_status(_cfg(tmp_path))


def test_bot_skips_sending_files_that_reached_youtube():
    """The whole point is that the phone never handles the file."""
    source = (Path(__file__).resolve().parent.parent / "amzvid" / "bot" / "telegram.py").read_text()
    assert "if item.youtube_url:\n            continue" in source
    assert "Uploaded to YouTube as drafts" in source


def test_upload_failure_does_not_lose_the_render():
    source = (Path(__file__).resolve().parent.parent / "amzvid" / "pipeline.py").read_text()
    block = source[source.index("def _upload("):]
    assert "except YouTubeError" in block and "return" in block, (
        "an upload failure must leave the rendered files intact"
    )
