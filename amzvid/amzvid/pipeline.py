"""Stage orchestration: assets -> script -> voice -> plan -> render -> metadata.

Split into two callable stages so the Telegram bot can put a human approval gate between
the script and everything that costs money or time.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import offline
from .config import LONG, SHORT, Canvas, Config, resolve_encoder
from .models import Cut, Product, RenderPlan, ScriptDraft, VoiceScene, Word
from .plan import build_plan
from .publish.metadata import build_metadata, shorts_description, write_metadata
from .publish.thumbnail import build_thumbnail
from .render import cards
from .render import ffmpeg as ffmpeg_tools
from .render.assemble import OverlaySpec, assemble_video, build_audio
from .render.captions import write_ass
from .render.scenes import SceneClip, render_scene_clips
from .sources import get_source
from .voice.elevenlabs import synthesize_scenes
from .voice.timing import shift

log = logging.getLogger(__name__)

CANVASES: dict[str, Canvas] = {LONG.name: LONG, SHORT.name: SHORT}
MUSIC_SUFFIXES = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}
OVERLAY_LEAD_IN = 0.25
TITLE_CARD_S = 2.2


@dataclass
class Deliverable:
    path: Path
    kind: str          # "long" | "short"
    title: str
    duration_s: float


# --------------------------------------------------------------------------- #
# Stage 1: assets + script
# --------------------------------------------------------------------------- #

def run_script_stage(cfg: Config, job_id: str, affiliate_url: str) -> tuple[Product, ScriptDraft]:
    job_dir = cfg.paths.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    product = get_source(cfg).fetch(affiliate_url, job_dir)
    log.info("product %s: %d images, %d bullets", product.asin, len(product.images), len(product.bullets))

    if cfg.dry_run:
        draft = offline.fake_draft(cfg, product)
    else:
        from .script.writer import write_script

        draft = write_script(cfg, product)

    (job_dir / "script.json").write_text(draft.model_dump_json(indent=2), encoding="utf-8")
    (job_dir / "product_resolved.json").write_text(
        json.dumps(
            {
                "asin": product.asin,
                "title": product.title,
                "brand": product.brand,
                "price": product.price,
                "affiliate_url": product.affiliate_url,
                "bullets": product.bullets,
                "images": [str(i.path) for i in product.images],
                "source": product.source,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return product, draft


def load_draft(cfg: Config, job_id: str) -> ScriptDraft:
    path = cfg.paths.job_dir(job_id) / "script.json"
    return ScriptDraft.model_validate_json(path.read_text())


# --------------------------------------------------------------------------- #
# Stage 2: voice + render
# --------------------------------------------------------------------------- #

def run_render_stage(
    cfg: Config, job_id: str, product: Product, draft: ScriptDraft
) -> list[Deliverable]:
    ffmpeg_tools.require_subtitles(cfg)  # before any of the expensive work

    job_dir = cfg.paths.job_dir(job_id)
    work = job_dir / "render"
    out_dir = cfg.paths.out_dir(job_id)
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    narrations = [s.narration for s in draft.scenes]
    voices: list[VoiceScene] = (
        offline.fake_voice(cfg, narrations, job_dir / "voice")
        if cfg.dry_run
        else synthesize_scenes(cfg, narrations, job_dir / "voice")
    )

    plan = build_plan(cfg, job_id, product, draft, voices)
    encoder = resolve_encoder(cfg)
    fonts = cards.resolve_fonts(cfg.paths.assets)
    music = pick_music(cfg, job_id)
    log.info("encoder=%s music=%s", encoder, music.name if music else "none")

    needed: dict[str, list[int]] = {}
    for cut in plan.cuts:
        needed.setdefault(cut.canvas_name, [])
        for i in cut.scene_indices:
            if i not in needed[cut.canvas_name]:
                needed[cut.canvas_name].append(i)

    clips: dict[str, dict[int, SceneClip]] = {}
    for canvas_name, indices in needed.items():
        canvas = CANVASES[canvas_name]
        subset = [plan.scene(i) for i in sorted(indices)]
        clips[canvas_name] = render_scene_clips(cfg, subset, canvas, work, encoder)

    deliverables: list[Deliverable] = []
    long_durations: list[float] = []
    for cut in plan.cuts:
        canvas = CANVASES[cut.canvas_name]
        result = _build_cut(cfg, plan, cut, canvas, clips[cut.canvas_name], work, out_dir, encoder, fonts, music)
        deliverables.append(result)
        if cut.name == "long":
            long_durations = [clips[cut.canvas_name][i].video_duration for i in cut.scene_indices]

    meta = build_metadata(cfg, plan, long_durations or [s.duration for s in plan.scenes])
    write_metadata(out_dir, "long", meta)
    for cut in plan.cuts:
        if cut.name != "long":
            (out_dir / f"{cut.name}.description.txt").write_text(
                shorts_description(cfg, product, cut.title), encoding="utf-8"
            )

    try:
        build_thumbnail(product.images[0].path, draft.thumbnail_text, fonts, out_dir / "thumbnail.jpg")
    except (OSError, ValueError) as exc:
        log.warning("thumbnail failed: %s", exc)

    _copy_source_images(product, out_dir / "stills")
    return deliverables


def _build_cut(
    cfg: Config,
    plan: RenderPlan,
    cut: Cut,
    canvas: Canvas,
    clips: dict[int, SceneClip],
    work: Path,
    out_dir: Path,
    encoder: str,
    fonts: cards.Fonts,
    music: Path | None,
) -> Deliverable:
    ordered = [plan.scene(i) for i in cut.scene_indices]
    ordered_clips = [clips[i] for i in cut.scene_indices]
    durations = [c.video_duration for c in ordered_clips]

    words: list[Word] = []
    starts: list[float] = []
    cursor = 0.0
    for scene, duration in zip(ordered, durations):
        starts.append(cursor)
        words.extend(shift(scene.words, cursor))
        cursor += duration

    captions = write_ass(work / f"captions_{cut.name}.ass", words, canvas, cfg.render)

    audio = build_audio(
        cfg,
        audio_paths=[s.audio_path for s in ordered if s.audio_path],
        durations=durations,
        dest=work / f"audio_{cut.name}.m4a",
        music=music,
        tail=cfg.render.transition_s,
    )

    overlays = _overlay_specs(cfg, plan, cut, canvas, ordered, starts, durations, work, fonts)

    dest = out_dir / f"{plan.product.asin}_{cut.name}.mp4"
    assemble_video(
        cfg,
        clips=ordered_clips,
        audio_path=work / f"audio_{cut.name}.m4a",
        captions_name=captions.name,
        overlays=overlays,
        canvas=canvas,
        encoder=encoder,
        dest=dest,
        work_dir=work,
        fonts_dir=cfg.paths.assets / "fonts",
    )
    return Deliverable(path=dest, kind=cut.canvas_name, title=cut.title, duration_s=audio)


def _overlay_specs(
    cfg: Config,
    plan: RenderPlan,
    cut: Cut,
    canvas: Canvas,
    ordered: list,
    starts: list[float],
    durations: list[float],
    work: Path,
    fonts: cards.Fonts,
) -> list[OverlaySpec]:
    specs: list[OverlaySpec] = []
    title_until = 0.0

    if cut.canvas_name == "short" and cut.title:
        path = work / f"title_{cut.name}.png"
        if not path.exists():
            cards.title_card(cut.title, canvas, fonts, path)
        title_until = min(TITLE_CARD_S, durations[0])
        specs.append(OverlaySpec(path, 0.0, title_until))

    for scene, start, duration in zip(ordered, starts, durations):
        window = (max(start + OVERLAY_LEAD_IN, title_until), start + duration - 0.1)
        if window[1] <= window[0]:
            continue
        if scene.overlay == "lower_third" and scene.on_screen_text:
            path = work / f"lt_{canvas.name}_{scene.index:02d}.png"
            if not path.exists():
                cards.lower_third(scene.on_screen_text, canvas, fonts, path)
        elif scene.overlay == "price" and plan.product.price:
            path = work / f"price_{canvas.name}_{scene.index:02d}.png"
            if not path.exists():
                cards.price_badge(plan.product.price, canvas, fonts, path)
        elif scene.overlay == "pros_cons" and (plan.draft.pros or plan.draft.cons):
            path = work / f"proscons_{canvas.name}_{scene.index:02d}.png"
            if not path.exists():
                cards.pros_cons(plan.draft.pros, plan.draft.cons, canvas, fonts, path)
        else:
            continue
        specs.append(OverlaySpec(path, *window))

    return specs


# --------------------------------------------------------------------------- #

def pick_music(cfg: Config, job_id: str) -> Path | None:
    """Deterministic per job, so a re-render keeps the same bed."""
    music_dir = cfg.paths.assets / "music"
    if not music_dir.exists():
        return None
    tracks = sorted(p for p in music_dir.iterdir() if p.suffix.lower() in MUSIC_SUFFIXES)
    if not tracks:
        return None
    digest = hashlib.sha256(job_id.encode()).digest()
    return tracks[digest[0] % len(tracks)]


def _copy_source_images(product: Product, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for image in product.images:
        try:
            shutil.copy2(image.path, dest / image.path.name)
        except OSError:
            continue
