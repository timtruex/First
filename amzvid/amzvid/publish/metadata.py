"""YouTube metadata: description, disclosure, chapters, tags.

Where the affiliate disclosure lands is set by `disclosure_position`. The Amazon
Associates Operating Agreement requires the statement, and the FTC requires it to be
clear and conspicuous wherever it sits, so the wording is positional rather than
assuming a spot. The script is also asked to say it aloud, which is the part a viewer
actually registers.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config import Config
from ..models import Product, RenderPlan

# "below" was only true while this sat at the top of the description. It is positional
# now, so the wording has to hold wherever it lands.
DISCLOSURE = (
    "As an Amazon Associate I earn from qualifying purchases. "
    "The links in this description are affiliate links, which means I may earn a "
    "commission at no extra cost to you."
)
PRICE_NOTE = (
    "Prices and availability shown in this video were accurate at the time of recording "
    "and can change at any time. Check the listing for the current price."
)
MIN_CHAPTER_S = 10.0
MIN_CHAPTERS = 3
#: Chapters on a one-minute video are a tell, not a convenience: four marks at thirteen
#: second intervals only tell a viewer the runtime was divided by the scene count.
#: YouTube itself does not show them on anything this short.
MIN_RUNTIME_FOR_CHAPTERS_S = 120.0

KIND_LABELS = {
    "hook": "Why this one",
    "feature": "What it does well",
    "spec": "The details",
    "con": "What is not great",
    "verdict": "Who should buy it",
    "cta": "Where to get it",
}


@dataclass
class VideoMetadata:
    title: str
    description: str
    tags: list[str]
    affiliate_url: str
    asin: str
    duration_s: float
    chapters: list[tuple[float, str]]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def build_chapters(plan: RenderPlan, durations: list[float]) -> list[tuple[float, str]]:
    """Merge scenes until each chapter clears YouTube's ten-second minimum."""
    chapters: list[tuple[float, str]] = []
    start = 0.0
    accrued = 0.0
    label = ""

    for scene, duration in zip(plan.scenes, durations):
        if not label:
            label = scene.on_screen_text.strip() or KIND_LABELS.get(scene.kind, "More")
        accrued += duration
        if accrued >= MIN_CHAPTER_S:
            chapters.append((start, label))
            start += accrued
            accrued = 0.0
            label = ""

    if accrued > 0 and chapters:
        chapters[-1] = (chapters[-1][0], chapters[-1][1])
    if sum(durations) < MIN_RUNTIME_FOR_CHAPTERS_S:
        return []
    return chapters if len(chapters) >= MIN_CHAPTERS else []


def build_metadata(cfg: Config, plan: RenderPlan, durations: list[float]) -> VideoMetadata:
    product = plan.product
    draft = plan.draft
    chapters = build_chapters(plan, durations)

    at_top = cfg.disclosure_position == "top"

    parts: list[str] = []
    if at_top:
        parts += [DISCLOSURE, ""]
    parts.append(f"👉 {product.short_title}: {product.affiliate_url}")
    parts.append("")
    parts.append(draft.youtube_description.strip())
    parts.append("")

    if draft.pros:
        parts.append("What stands out, from the listing:")
        parts.extend(f"• {p}" for p in draft.pros)
        parts.append("")
    if draft.cons:
        parts.append("Worth checking before you buy:")
        parts.extend(f"• {c}" for c in draft.cons)
        parts.append("")

    parts.append(f"Verdict: {draft.verdict.strip()}")
    parts.append("")

    if chapters:
        parts.append("Chapters")
        parts.extend(f"{timestamp(at)} {label}" for at, label in chapters)
        parts.append("")

    parts.append(PRICE_NOTE)
    if not at_top:
        parts += ["", DISCLOSURE]
    if cfg.channel_name:
        parts += ["", f"— {cfg.channel_name}"]

    return VideoMetadata(
        title=draft.youtube_title.strip(),
        description="\n".join(parts).strip(),
        tags=draft.tags,
        affiliate_url=product.affiliate_url,
        asin=product.asin,
        duration_s=round(sum(durations), 2),
        chapters=chapters,
    )


def shorts_description(cfg: Config, product: Product, title: str) -> str:
    at_top = cfg.disclosure_position == "top"
    lines = [title.strip(), ""]
    if at_top:
        lines.append(DISCLOSURE)
    lines += [
        f"👉 {product.short_title}: {product.affiliate_url}",
        "",
        PRICE_NOTE,
        "",
        "#shorts",
    ]
    if not at_top:
        lines += ["", DISCLOSURE]
    if cfg.channel_name:
        lines += ["", f"— {cfg.channel_name}"]
    return "\n".join(lines)


def write_metadata(out_dir: Path, name: str, meta: VideoMetadata) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{name}.metadata.json"
    txt_path = out_dir / f"{name}.description.txt"
    json_path.write_text(meta.to_json(), encoding="utf-8")
    txt_path.write_text(
        f"TITLE\n{meta.title}\n\nTAGS\n{', '.join(meta.tags)}\n\nDESCRIPTION\n{meta.description}\n",
        encoding="utf-8",
    )
    return json_path, txt_path
