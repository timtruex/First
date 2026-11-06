"""ffmpeg process handling and encoder selection."""
from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from ..config import Config

log = logging.getLogger(__name__)

BASE = ("-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-stats_period", "10")


class FFmpegError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        tail = "\n".join(stderr.strip().splitlines()[-25:])
        super().__init__(f"ffmpeg exited {returncode}\n  cmd: {shlex.join(cmd)}\n{tail}")
        self.returncode = returncode
        self.stderr = stderr


def run(cfg: Config, args: list[str], *, cwd: Path | None = None, timeout: int = 3600) -> None:
    cmd = [cfg.ffmpeg, *BASE, *args]
    log.debug("ffmpeg: %s", shlex.join(cmd))
    proc = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr)


def video_encoder_args(cfg: Config, encoder: str) -> list[str]:
    """VideoToolbox has no CRF mode, so it is driven by a bitrate ceiling instead."""
    if encoder == "h264_videotoolbox":
        rate = cfg.render.video_bitrate
        return [
            "-c:v", "h264_videotoolbox",
            "-b:v", rate,
            "-maxrate", rate,
            "-bufsize", _double_rate(rate),
            "-profile:v", "high",
            "-allow_sw", "1",
            "-pix_fmt", "yuv420p",
        ]
    return [
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
    ]


def intermediate_encoder_args(cfg: Config, encoder: str) -> list[str]:
    """Scene clips are re-encoded once more during assembly, so keep them near-lossless."""
    if encoder == "h264_videotoolbox":
        return [
            "-c:v", "h264_videotoolbox",
            "-b:v", "40M",
            "-maxrate", "40M",
            "-bufsize", "80M",
            "-profile:v", "high",
            "-allow_sw", "1",
            "-pix_fmt", "yuv420p",
        ]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p"]


def has_filter(cfg: Config, name: str) -> bool:
    """Whether this ffmpeg build carries a filter. Cached: ffmpeg -filters is not free."""
    cache = _FILTER_CACHE.setdefault(cfg.ffmpeg, None)
    if cache is None:
        try:
            out = subprocess.run(
                [cfg.ffmpeg, "-hide_banner", "-filters"],
                capture_output=True, text=True, timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        cache = {line.split()[1] for line in out.splitlines() if len(line.split()) > 1}
        _FILTER_CACHE[cfg.ffmpeg] = cache
    return name in cache


_FILTER_CACHE: dict[str, set[str] | None] = {}


def require_subtitles(cfg: Config) -> None:
    """Fail before rendering, not after.

    Captions are burned in the very last filter of the very last pass, so an ffmpeg
    built without libass wastes the whole render before it reports anything.
    """
    if has_filter(cfg, "subtitles"):
        return
    raise FFmpegError(
        [cfg.ffmpeg],
        1,
        "this ffmpeg has no 'subtitles' filter, so captions cannot be burned in.\n"
        "It was built without libass. On macOS:  brew reinstall ffmpeg\n"
        "Then check it took:  ffmpeg -filters | grep subtitles",
    )


def escape_filter_path(path: Path | str) -> str:
    """Filtergraph paths need backslash, colon and quote escaped, in that order."""
    text = str(path)
    for char in ("\\", ":", "'", "[", "]", ","):
        text = text.replace(char, f"\\{char}")
    return text


def _double_rate(rate: str) -> str:
    unit = rate[-1] if rate and rate[-1].isalpha() else ""
    try:
        value = float(rate[: -1] if unit else rate)
    except ValueError:
        return rate
    return f"{value * 2:g}{unit}"
