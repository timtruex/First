"""Turn a script draft plus voiced audio into a concrete render plan.

Image assignment matters more than it looks. The hero photo carries the hook and the
verdict because those are the frames a viewer decides on; detail photos carry the feature
scenes. When there are fewer photos than scenes the sequence reuses them with a different
move, which reads as a second look rather than as a repeat.
"""
from __future__ import annotations

from .config import Config
from .models import Cut, PlannedScene, Product, RenderPlan, ScriptDraft, VoiceScene
from .render.scenes import choose_moves


def assign_images(product: Product, kinds: list[str]) -> list:
    """Hero for the frames that sell; details for the frames that explain."""
    if not product.images:
        raise ValueError("product has no images")
    hero = product.images[0].path
    details = [img.path for img in product.images[1:]] or [hero]

    out = []
    cursor = 0
    for kind in kinds:
        if kind in ("hook", "verdict", "cta"):
            out.append(hero)
        else:
            out.append(details[cursor % len(details)])
            cursor += 1
    return out


def choose_overlays(draft: ScriptDraft, product: Product) -> list[str]:
    """One overlay per scene at most. Stacking two is noise, not production value."""
    overlays: list[str] = []
    price_placed = False
    for scene in draft.scenes:
        if scene.kind == "con" and (draft.pros or draft.cons):
            overlays.append("pros_cons")
        elif scene.kind == "cta" and product.price and not price_placed:
            overlays.append("price")
            price_placed = True
        elif scene.on_screen_text.strip():
            overlays.append("lower_third")
        else:
            overlays.append("none")

    if product.price and not price_placed:
        # Put the price on the first scene that has nothing else to say.
        for i, overlay in enumerate(overlays):
            if overlay == "none" and draft.scenes[i].kind != "hook":
                overlays[i] = "price"
                break
    return overlays


def build_plan(
    cfg: Config,
    job_id: str,
    product: Product,
    draft: ScriptDraft,
    voices: list[VoiceScene],
) -> RenderPlan:
    if len(voices) != len(draft.scenes):
        raise ValueError(f"voiced {len(voices)} scenes but the script has {len(draft.scenes)}")

    kinds = [s.kind for s in draft.scenes]
    images = assign_images(product, kinds)
    moves = choose_moves(len(draft.scenes), kinds)
    overlays = choose_overlays(draft, product)

    scenes = [
        PlannedScene(
            index=i,
            kind=draft.scenes[i].kind,
            narration=draft.scenes[i].narration,
            image=images[i],
            move=moves[i],
            duration=voices[i].duration,
            on_screen_text=draft.scenes[i].on_screen_text.strip(),
            overlay=overlays[i],
            words=voices[i].words,
            audio_path=voices[i].audio_path,
        )
        for i in range(len(draft.scenes))
    ]

    cuts = [Cut(name="long", canvas_name="long", scene_indices=[s.index for s in scenes], title=draft.youtube_title)]
    for n, short in enumerate(draft.shorts[: cfg.shorts_per_video], start=1):
        cuts.append(
            Cut(
                name=f"short{n}",
                canvas_name="short",
                scene_indices=list(short.scene_indices),
                title=short.title,
            )
        )

    return RenderPlan(job_id=job_id, product=product, draft=draft, scenes=scenes, cuts=cuts)


def script_preview(plan_draft: ScriptDraft) -> str:
    """Plain-text script for the Telegram approval step."""
    lines = []
    for i, scene in enumerate(plan_draft.scenes):
        tag = scene.kind.upper()
        lines.append(f"{i}. [{tag}] {scene.narration}")
        if scene.on_screen_text:
            lines.append(f"    on screen: {scene.on_screen_text}")
    lines += [
        "",
        f"Pros: {'; '.join(plan_draft.pros)}",
        f"Cons: {'; '.join(plan_draft.cons)}",
        f"Verdict: {plan_draft.verdict}",
        "",
        f"Title: {plan_draft.youtube_title}",
        f"Thumbnail: {plan_draft.thumbnail_text}",
    ]
    for short in plan_draft.shorts:
        lines.append(f"Short: {short.title} (scenes {', '.join(map(str, short.scene_indices))})")
    return "\n".join(lines)
