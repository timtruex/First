"""Domain models. ScriptDraft is the LLM contract; everything else is internal state."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SceneKind = Literal["hook", "feature", "spec", "con", "verdict", "cta"]
Move = Literal[
    "zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down",
    "zoom_in_pan_right", "zoom_in_pan_left", "static",
]
OverlayKind = Literal["none", "price", "lower_third", "pros_cons", "spec_table"]


# --------------------------------------------------------------------------- #
# Product assets
# --------------------------------------------------------------------------- #

@dataclass
class ProductImage:
    path: Path
    width: int = 0
    height: int = 0

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 1.0


@dataclass
class Product:
    asin: str
    title: str
    affiliate_url: str
    images: list[ProductImage] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    brand: str = ""
    price: str = ""
    rating: str = ""
    review_count: str = ""
    researcher_notes: str = ""
    source: str = "manual"

    @property
    def short_title(self) -> str:
        """Amazon titles are keyword soup. Cut at the first separator for on-screen use."""
        head = re.split(r",|\s*[|(\[]|\s[-–—]\s|\s{2,}", self.title.strip())[0]
        words = head.split()
        return " ".join(words[:8]) if len(words) > 8 else head


# --------------------------------------------------------------------------- #
# LLM contract
# --------------------------------------------------------------------------- #

class DraftScene(BaseModel):
    narration: str = Field(description="One or two spoken sentences. No stage directions, no emoji.")
    kind: SceneKind
    on_screen_text: str = Field(
        default="", description="At most 5 words for a lower-third caption. Empty for none."
    )
    image_hint: str = Field(
        default="", description="Which product photo this should sit over, in a few words."
    )

    @field_validator("narration")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = re.sub(r"\s+", " ", v).strip()
        if not v:
            raise ValueError("narration must not be empty")
        return v


class DraftShort(BaseModel):
    title: str = Field(description="Punchy Shorts title under 60 characters.")
    scene_indices: list[int] = Field(description="Indices into scenes, in order, 2-4 of them.")


class ScriptDraft(BaseModel):
    """Structured output from Claude. Field order is the order the model fills them in."""

    scenes: list[DraftScene]
    pros: list[str] = Field(description="Exactly 3 concrete pros, max 6 words each.")
    cons: list[str] = Field(description="1 or 2 honest drawbacks, max 6 words each.")
    verdict: str = Field(description="One sentence: who should buy this.")
    youtube_title: str = Field(description="Under 70 characters, front-loaded with the product.")
    youtube_description: str = Field(description="2-4 short paragraphs. No links, no hashtags.")
    tags: list[str] = Field(description="8-14 YouTube tags, lowercase.")
    shorts: list[DraftShort]
    thumbnail_text: str = Field(description="2-4 words, all caps, for the thumbnail.")


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float

    def shifted(self, dt: float) -> "Word":
        return Word(self.text, self.start + dt, self.end + dt)


@dataclass
class VoiceScene:
    """One scene's narration rendered to audio, with word-level timing local to that file."""

    index: int
    audio_path: Path
    duration: float
    words: list[Word]


# --------------------------------------------------------------------------- #
# Render plan
# --------------------------------------------------------------------------- #

@dataclass
class PlannedScene:
    index: int
    kind: SceneKind
    narration: str
    image: Path
    move: Move
    duration: float            # audio duration, before the transition tail
    on_screen_text: str = ""
    overlay: OverlayKind = "none"
    words: list[Word] = field(default_factory=list)
    audio_path: Path | None = None


@dataclass
class Cut:
    """One deliverable video: an ordered subset of scenes rendered to one canvas."""

    name: str
    canvas_name: str
    scene_indices: list[int]
    title: str = ""


@dataclass
class RenderPlan:
    job_id: str
    product: Product
    draft: ScriptDraft
    scenes: list[PlannedScene]
    cuts: list[Cut]

    def scene(self, index: int) -> PlannedScene:
        return next(s for s in self.scenes if s.index == index)

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.scenes)
