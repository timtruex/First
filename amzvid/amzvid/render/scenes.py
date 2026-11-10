"""Pass A: one motion clip per scene per canvas.

Rendering scenes as separate clips rather than as one giant filtergraph buys three things:
the renders parallelize across cores, a failure names the scene that failed instead of
dying at frame 4000, and a Short can reuse a scene's clip without re-deriving its motion.

Frame counts are exact. Clip length is the scene's frame-aligned duration plus one
transition's worth of tail, which is what the cross-fade in Pass B consumes.
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from ..config import Canvas, Config
from ..models import Move, PlannedScene
from . import ffmpeg
from .compose import build_plate, plate_path
from .kenburns import zoompan_filter

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SceneClip:
    index: int
    path: Path
    video_duration: float   # frame-aligned, excludes the transition tail
    frames: int             # frames in the file, including the tail


def frame_aligned_duration(audio_duration: float, gap: float, canvas: Canvas) -> tuple[int, float]:
    """Round a scene up to whole frames so audio and video never drift apart."""
    frames = max(2, math.ceil((audio_duration + gap) * canvas.fps))
    return frames, frames / canvas.fps


def render_scene_clips(
    cfg: Config,
    scenes: list[PlannedScene],
    canvas: Canvas,
    work_dir: Path,
    encoder: str,
) -> dict[int, SceneClip]:
    """Render every scene for one canvas, in parallel, returning clips keyed by scene index."""
    work_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for scene in scenes:
        frames, vdur = frame_aligned_duration(scene.duration, cfg.voice.inter_scene_gap_s, canvas)
        tail = round(cfg.render.transition_s * canvas.fps)
        jobs.append((scene, frames, vdur, frames + tail))

    clips: dict[int, SceneClip] = {}
    with ThreadPoolExecutor(max_workers=max(1, cfg.render.jobs)) as pool:
        futures = {
            pool.submit(_render_one, cfg, scene, canvas, work_dir, encoder, total): (scene, vdur, total)
            for scene, _frames, vdur, total in jobs
        }
        for future in as_completed(futures):
            scene, vdur, total = futures[future]
            path = future.result()
            clips[scene.index] = SceneClip(scene.index, path, vdur, total)
            log.info("rendered %s scene %d (%d frames)", canvas.name, scene.index, total)
    return clips


def _render_one(
    cfg: Config,
    scene: PlannedScene,
    canvas: Canvas,
    work_dir: Path,
    encoder: str,
    total_frames: int,
) -> Path:
    plate = plate_path(work_dir, canvas, scene.index)
    if not plate.exists():
        build_plate(scene.image, plate, canvas)

    dest = work_dir / f"clip_{canvas.name}_{scene.index:02d}.mp4"
    duration = total_frames / canvas.fps
    motion = zoompan_filter(scene.move, duration, canvas)

    ffmpeg.run(
        cfg,
        [
            "-framerate", str(canvas.fps),
            "-loop", "1",
            "-i", str(plate),
            "-frames:v", str(total_frames),
            "-vf", f"{motion},format=yuv420p",
            "-an",
            "-r", str(canvas.fps),
            *ffmpeg.intermediate_encoder_args(cfg, encoder),
            str(dest),
        ],
        timeout=1800,
    )
    return dest


def choose_moves(count: int, kinds: list[str]) -> list[Move]:
    """Hook and verdict push in; the rest alternate so no two neighbours move alike."""
    from .kenburns import pick_moves

    moves = pick_moves(count)
    for i, kind in enumerate(kinds[:count]):
        if kind == "hook":
            moves[i] = "zoom_in"
        elif kind == "verdict":
            moves[i] = "zoom_out"
    for i in range(1, count):
        if moves[i] == moves[i - 1]:
            moves[i] = "zoom_in" if moves[i - 1] != "zoom_in" else "pan_right"
    return moves
