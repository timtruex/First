"""Pass B: build the mixed audio bed, then cross-fade the scene clips into a final file.

Audio is produced in its own pass. Doing it inline would mean one filtergraph holding
2N+ inputs, which is hard to debug and easy to get wrong; a separate pass makes the voice
bed an inspectable artifact you can listen to before committing to a render.

Cross-fade offsets follow from the clip lengths. With every clip cut to its frame-aligned
duration plus one transition's tail, the k-th xfade sits at the sum of the durations
before it, and the finished video runs sum(durations) + one transition of tail.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Canvas, Config
from . import ffmpeg
from .scenes import SceneClip

log = logging.getLogger(__name__)

DUCK_THRESHOLD = 0.04


@dataclass(frozen=True)
class OverlaySpec:
    path: Path
    start: float
    end: float


def build_audio(
    cfg: Config,
    *,
    audio_paths: list[Path],
    durations: list[float],
    dest: Path,
    music: Path | None,
    tail: float,
) -> float:
    """Concatenate scene audio onto a frame-aligned grid, then duck music under it."""
    if len(audio_paths) != len(durations):
        raise ValueError("audio_paths and durations must line up")
    if not audio_paths:
        raise ValueError("no scene audio to assemble")

    total = sum(durations) + tail
    args: list[str] = []
    for path in audio_paths:
        args += ["-i", str(path)]
    if music:
        args += ["-stream_loop", "-1", "-i", str(music)]

    chain: list[str] = []
    for i, duration in enumerate(durations):
        chain.append(
            f"[{i}:a]aresample=48000,atrim=duration={duration:.4f},"
            f"apad=whole_dur={duration:.4f},asetpts=N/SR/TB[a{i}]"
        )
    labels = "".join(f"[a{i}]" for i in range(len(durations)))
    chain.append(f"{labels}concat=n={len(durations)}:v=0:a=1[voice]")
    chain.append(f"[voice]apad=pad_dur={tail:.4f}" + (",asplit=2[v1][v2]" if music else "[v1]"))

    if music:
        gain = cfg.render.music_gain_db
        fade_out_at = max(0.0, total - 2.0)
        chain.append(
            f"[{len(durations)}:a]aresample=48000,atrim=duration={total:.4f},asetpts=N/SR/TB,"
            f"volume={gain}dB,afade=t=in:st=0:d=1.5,"
            f"afade=t=out:st={fade_out_at:.4f}:d=2[mus]"
        )
        chain.append(
            f"[mus][v2]sidechaincompress=threshold={DUCK_THRESHOLD}:"
            f"ratio={cfg.render.duck_ratio}:attack=20:release=400[duck]"
        )
        chain.append("[v1][duck]amix=inputs=2:duration=longest:normalize=0[mixed]")
        chain.append("[mixed]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        chain.append("[v1]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")

    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        cfg,
        [
            *args,
            "-filter_complex", ";".join(chain),
            "-map", "[aout]",
            "-c:a", "aac",
            "-b:a", cfg.render.audio_bitrate,
            "-ar", "48000",
            "-ac", "2",
            str(dest),
        ],
        timeout=1800,
    )
    return total


def assemble_video(
    cfg: Config,
    *,
    clips: list[SceneClip],
    audio_path: Path,
    captions_name: str | None,
    overlays: list[OverlaySpec],
    canvas: Canvas,
    encoder: str,
    dest: Path,
    work_dir: Path,
    fonts_dir: Path | None,
) -> Path:
    if not clips:
        raise ValueError("no clips to assemble")

    transition = cfg.render.transition_s
    total = sum(c.video_duration for c in clips) + transition

    args: list[str] = []
    for clip in clips:
        args += ["-i", str(clip.path)]
    audio_index = len(clips)
    args += ["-i", str(audio_path)]

    chain: list[str] = []
    current = "0:v"
    offset = 0.0
    for i in range(1, len(clips)):
        offset += clips[i - 1].video_duration
        label = f"x{i}"
        chain.append(
            f"[{current}][{i}:v]xfade=transition=fade:"
            f"duration={transition:.4f}:offset={offset:.4f}[{label}]"
        )
        current = label

    overlay_index = audio_index + 1
    for n, spec in enumerate(overlays):
        args += ["-i", str(spec.path)]
        label = f"ov{n}"
        chain.append(
            f"[{current}][{overlay_index + n}:v]overlay=0:0:format=auto:"
            f"enable='between(t,{spec.start:.3f},{spec.end:.3f})'[{label}]"
        )
        current = label

    tail = []
    if captions_name:
        subtitle = f"subtitles=filename={captions_name}"
        if fonts_dir and fonts_dir.exists():
            subtitle += f":fontsdir={ffmpeg.escape_filter_path(fonts_dir)}"
        tail.append(subtitle)
    tail.append("format=yuv420p")
    chain.append(f"[{current}]{','.join(tail)}[vout]")

    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        cfg,
        [
            *args,
            "-filter_complex", ";".join(chain),
            "-map", "[vout]",
            "-map", f"{audio_index}:a",
            *ffmpeg.video_encoder_args(cfg, encoder, ffmpeg.fitted_bitrate(cfg, total)),
            "-c:a", "aac",
            "-b:a", cfg.render.audio_bitrate,
            "-r", str(canvas.fps),
            "-t", f"{total:.4f}",
            "-movflags", "+faststart",
            str(dest),
        ],
        cwd=work_dir,
        timeout=5400,
    )
    log.info("wrote %s (%.1fs)", dest.name, total)
    return dest
