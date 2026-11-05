"""Configuration loading: TOML file plus environment overrides for secrets."""
from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(os.environ.get("AMZVID_CONFIG", "~/.config/amzvid/config.toml")).expanduser()


class ConfigError(RuntimeError):
    """Raised when configuration is missing or internally inconsistent."""


@dataclass(frozen=True)
class Canvas:
    """One output geometry. Scene clips are rendered per-canvas, never cropped after the fact."""

    name: str
    width: int
    height: int
    fps: int = 30

    @property
    def aspect(self) -> float:
        return self.width / self.height


LONG = Canvas("long", 1920, 1080, 30)
SHORT = Canvas("short", 1080, 1920, 30)


@dataclass(frozen=True)
class VoiceConfig:
    api_key: str
    voice_id: str
    model_id: str = "eleven_multilingual_v2"
    output_format: str = "mp3_44100_128"
    stability: float = 0.45
    similarity_boost: float = 0.75
    style: float = 0.15
    speed: float = 1.0
    base_url: str = "https://api.elevenlabs.io"
    inter_scene_gap_s: float = 0.22


@dataclass(frozen=True)
class ScriptConfig:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    target_words: int = 190
    max_scenes: int = 10
    min_scenes: int = 6
    channel_voice: str = (
        "Direct, specific, mildly skeptical. Sounds like a person who actually used the thing."
    )


@dataclass(frozen=True)
class RenderConfig:
    encoder: str = "auto"  # auto | h264_videotoolbox | libx264
    video_bitrate: str = "12M"
    audio_bitrate: str = "192k"
    transition_s: float = 0.45
    caption_font: str = "Arial Black"
    caption_size_long: int = 68
    caption_size_short: int = 76
    caption_words_per_chunk: int = 4
    primary_color: str = "&H00FFFFFF"  # ASS BGR order
    highlight_color: str = "&H0036D7FF"  # amber highlight for the spoken word
    outline_color: str = "&H00101010"
    music_gain_db: float = -22.0
    duck_ratio: float = 8.0
    jobs: int = 4  # parallel scene renders


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    allowed_user_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Paths:
    work: Path
    output: Path
    db: Path
    assets: Path

    def job_dir(self, job_id: str) -> Path:
        return self.work / job_id

    def out_dir(self, job_id: str) -> Path:
        return self.output / job_id


@dataclass(frozen=True)
class Config:
    paths: Paths
    voice: VoiceConfig
    telegram: TelegramConfig | None
    script: ScriptConfig = field(default_factory=ScriptConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    affiliate_tag: str = ""
    channel_name: str = ""
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    dry_run: bool = False
    source: str = "manual"  # manual | paapi
    paapi: dict[str, str] = field(default_factory=dict)
    shorts_per_video: int = 3

    def require_affiliate_tag(self) -> str:
        if not self.affiliate_tag:
            raise ConfigError("affiliate_tag is not set; add it to [general] in config.toml")
        return self.affiliate_tag


def _subset(cls: type, data: dict[str, Any], **extra: Any) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown keys for [{cls.__name__}]: {', '.join(sorted(unknown))}")
    return cls(**{**data, **extra})


def load_config(path: Path | None = None) -> Config:
    cfg_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(
            f"no config at {cfg_path}. Copy config.example.toml there and fill it in, "
            "or set AMZVID_CONFIG to another path."
        )
    raw = tomllib.loads(cfg_path.read_text())

    general = dict(raw.get("general", {}))
    work = Path(general.pop("work_dir", "~/Library/Caches/amzvid")).expanduser()
    output = Path(general.pop("output_dir", "~/Movies/amzvid")).expanduser()
    db = Path(general.pop("db_path", work / "jobs.sqlite3")).expanduser()
    assets = Path(general.pop("assets_dir", Path(__file__).resolve().parent.parent / "assets")).expanduser()
    for d in (work, output, db.parent):
        d.mkdir(parents=True, exist_ok=True)

    voice_raw = dict(raw.get("voice", {}))
    api_key = os.environ.get("ELEVENLABS_API_KEY", voice_raw.pop("api_key", ""))
    if not api_key:
        raise ConfigError("set ELEVENLABS_API_KEY in the environment or voice.api_key in config.toml")
    if not voice_raw.get("voice_id"):
        raise ConfigError("voice.voice_id is required (copy it from the ElevenLabs voice library)")

    tg_raw = dict(raw.get("telegram", {}))
    token = os.environ.get("TELEGRAM_BOT_TOKEN", tg_raw.pop("bot_token", ""))
    telegram = None
    if token:
        ids = tuple(int(x) for x in tg_raw.pop("allowed_user_ids", []))
        telegram = _subset(TelegramConfig, tg_raw, bot_token=token, allowed_user_ids=ids)

    return Config(
        paths=Paths(work=work, output=output, db=db, assets=assets),
        voice=_subset(VoiceConfig, voice_raw, api_key=api_key),
        telegram=telegram,
        script=_subset(ScriptConfig, dict(raw.get("script", {}))),
        render=_subset(RenderConfig, dict(raw.get("render", {}))),
        paapi=dict(raw.get("paapi", {})),
        **general,
    )


def resolve_encoder(cfg: Config) -> str:
    """Pick a hardware encoder when one exists. VideoToolbox on Apple Silicon is 5-10x libx264."""
    if cfg.render.encoder != "auto":
        return cfg.render.encoder
    if not shutil.which(cfg.ffmpeg):
        raise ConfigError(f"{cfg.ffmpeg} not found on PATH. Install with: brew install ffmpeg")
    try:
        out = subprocess.run(
            [cfg.ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "libx264"
    return "h264_videotoolbox" if "h264_videotoolbox" in out else "libx264"
