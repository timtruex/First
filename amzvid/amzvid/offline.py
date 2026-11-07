"""Offline stand-ins for the two paid APIs, used when dry_run is on.

The point is to exercise the whole render path - plates, motion, caption timing, the
frame-aligned audio grid, overlays, assembly - without spending a cent, so a change to
the renderer can be verified before any real job runs through it.

Word timings are estimated from a syllable-weighted speaking rate rather than a flat
words-per-minute figure, which keeps the fake captions close enough to real ones that
layout problems still show up.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .models import DraftShort, DraftScene, Product, ScriptDraft, VoiceScene, Word
from .render import ffmpeg

WORDS_PER_SECOND = 2.6
VOWEL_GROUPS = re.compile(r"[aeiouy]+", re.I)


def fake_draft(cfg: Config, product: Product) -> ScriptDraft:
    bullets = product.bullets or [
        "It does the one job it claims to do.",
        "The build feels better than the price suggests.",
        "Setup took under five minutes.",
    ]
    name = product.short_title
    scenes = [
        DraftScene(
            narration=f"The {name} does one thing really well, and here is what that is.",
            kind="hook",
            on_screen_text="The Problem",
        )
    ]
    for bullet in bullets[:4]:
        scenes.append(
            DraftScene(
                narration=_to_sentence(bullet),
                kind="feature",
                on_screen_text="",
            )
        )
    scenes += [
        DraftScene(
            narration="Worth knowing: the instructions are thin, so expect to work it out yourself.",
            kind="con",
            on_screen_text="",
        ),
        DraftScene(
            narration=f"If you want a {name} that just works, this one earns its place.",
            kind="verdict",
            on_screen_text="The Verdict",
        ),
        DraftScene(
            narration="The link is in the description if you want to take a closer look.",
            kind="cta",
            on_screen_text="",
        ),
    ]
    indices = list(range(len(scenes)))
    return ScriptDraft(
        scenes=scenes,
        pros=["Does the job", "Solid build", "Quick setup"],
        cons=["Thin instructions"],
        verdict=f"Worth it if you need a dependable {name.lower()}.",
        youtube_title=f"{name} Review - Is It Worth It?",
        youtube_description=(
            "A short, honest look at this one.\n\nThis description was generated in dry-run "
            "mode and is placeholder text."
        ),
        tags=[name.lower(), "review", "amazon finds", "worth it", "tested"],
        shorts=[
            DraftShort(title=f"{name} in 30 seconds", scene_indices=indices[:3]),
            DraftShort(title="The one downside", scene_indices=indices[-3:-1]),
            DraftShort(title="Should you buy it?", scene_indices=indices[-2:]),
        ],
        thumbnail_text="WORTH IT?",
    )


def fake_voice(cfg: Config, narrations: list[str], out_dir: Path) -> list[VoiceScene]:
    """Silent audio of a plausible length, with estimated word timings."""
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes: list[VoiceScene] = []
    for i, text in enumerate(narrations):
        words = _estimate_words(text)
        duration = (words[-1].end if words else 1.0) + 0.25
        path = out_dir / f"scene_{i:02d}.mp3"
        if not path.exists():
            _silence(cfg, path, duration)
        scenes.append(VoiceScene(index=i, audio_path=path, duration=duration, words=words))
    return scenes


def _estimate_words(text: str) -> list[Word]:
    tokens = text.split()
    if not tokens:
        return []
    weights = [max(1, len(VOWEL_GROUPS.findall(t)) or 1) for t in tokens]
    total_weight = sum(weights)
    span = len(tokens) / WORDS_PER_SECOND
    cursor = 0.0
    words: list[Word] = []
    for token, weight in zip(tokens, weights):
        length = span * (weight / total_weight)
        words.append(Word(token, cursor, cursor + length * 0.92))
        cursor += length
    return words


def _silence(cfg: Config, dest: Path, duration: float) -> None:
    ffmpeg.run(
        cfg,
        [
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", f"{duration:.4f}",
            "-c:a", "libmp3lame", "-b:a", "128k",
            str(dest),
        ],
        timeout=300,
    )


def _to_sentence(bullet: str) -> str:
    text = re.sub(r"\s+", " ", bullet).strip().rstrip(".")
    text = re.sub(r"^[\W_]+", "", text)
    if not text:
        return "It works as described."
    sentence = text[0].upper() + text[1:]
    words = sentence.split()
    if len(words) > 26:
        sentence = " ".join(words[:26])
    return sentence + "."
