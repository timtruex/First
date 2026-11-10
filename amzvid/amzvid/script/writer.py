"""Claude-backed script writer.

Uses structured outputs so the pipeline never parses prose. Optional request parameters
are stripped one at a time if the installed SDK or the model rejects them, so a version
skew degrades the request instead of killing the job.
"""
from __future__ import annotations

import logging
from typing import Any

import anthropic

from ..config import Config
from ..models import Product, ScriptDraft
from .prompts import SYSTEM, build_user_prompt

log = logging.getLogger(__name__)

# Dropped in this order when the API rejects the request shape.
OPTIONAL_KEYS = ("fallbacks", "betas", "output_config", "thinking")


class ScriptError(RuntimeError):
    pass


def write_script(cfg: Config, product: Product) -> ScriptDraft:
    client = anthropic.Anthropic()
    kwargs: dict[str, Any] = {
        "model": cfg.script.model,
        "max_tokens": 16000,
        "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": build_user_prompt(cfg, product)}],
        "output_format": ScriptDraft,
        "output_config": {"effort": cfg.script.effort},
        "thinking": {"type": "adaptive"},
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }

    response = _call_with_degradation(client, kwargs)

    if getattr(response, "stop_reason", None) == "refusal":
        detail = getattr(response, "stop_details", None)
        category = getattr(detail, "category", None) or "unspecified"
        raise ScriptError(
            f"the model declined to write this script (category: {category}). "
            "Check the researcher notes for anything that reads as a medical or safety claim."
        )
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise ScriptError("script generation hit max_tokens before finishing; retry the job")

    draft = response.parsed_output
    if draft is None:
        raise ScriptError("structured output came back empty")
    return _validate(cfg, draft)


def _call_with_degradation(client: anthropic.Anthropic, kwargs: dict[str, Any]):
    attempt = dict(kwargs)
    last: Exception | None = None
    for dropped in (None, *OPTIONAL_KEYS):
        if dropped is not None:
            if dropped not in attempt:
                continue
            attempt.pop(dropped)
            log.warning("retrying script request without %r", dropped)
        try:
            return client.messages.parse(**attempt)
        except (anthropic.BadRequestError, TypeError) as exc:
            last = exc
            continue
        except anthropic.APIStatusError:
            raise
    raise ScriptError(f"could not build an acceptable request for the script writer: {last}")


def _validate(cfg: Config, draft: ScriptDraft) -> ScriptDraft:
    """Enforce the structural rules the prompt asks for but cannot guarantee."""
    if len(draft.scenes) < 3:
        raise ScriptError(f"script came back with only {len(draft.scenes)} scenes")
    if len(draft.scenes) > cfg.script.max_scenes:
        draft.scenes = draft.scenes[: cfg.script.max_scenes]

    kinds = [s.kind for s in draft.scenes]
    if "hook" not in kinds:
        draft.scenes[0].kind = "hook"
    if "cta" not in kinds:
        draft.scenes[-1].kind = "cta"

    draft.pros = [p.strip() for p in draft.pros if p.strip()][:3]
    draft.cons = [c.strip() for c in draft.cons if c.strip()][:2]
    draft.tags = _dedupe_lower(draft.tags)[:14]
    draft.thumbnail_text = draft.thumbnail_text.strip().upper()[:28]
    if len(draft.youtube_title) > 100:
        draft.youtube_title = draft.youtube_title[:97].rstrip() + "..."

    draft.shorts = _clamp_shorts(cfg, draft)
    return draft


def _clamp_shorts(cfg: Config, draft: ScriptDraft) -> list:
    """Drop out-of-range indices; fall back to a sliding window if the model's cuts are unusable."""
    n = len(draft.scenes)
    valid = []
    for short in draft.shorts:
        idx = [i for i in dict.fromkeys(short.scene_indices) if 0 <= i < n]
        if len(idx) >= 2:
            short.scene_indices = idx[:4]
            short.title = short.title.strip()[:95] or "Quick look"
            valid.append(short)

    wanted = cfg.shorts_per_video
    if len(valid) >= wanted:
        return valid[:wanted]

    hook = 0
    body = [i for i, s in enumerate(draft.scenes) if s.kind in ("feature", "spec", "con")]
    verdict = [i for i, s in enumerate(draft.scenes) if s.kind in ("verdict", "cta")]
    candidates = [
        ([hook, *body[:2]], draft.scenes[hook].on_screen_text or "The hook"),
        (body[1:4] or body[:3], "Three things to know"),
        ([*body[-1:], *verdict[:1]], "The verdict"),
    ]
    from ..models import DraftShort

    for idx, title in candidates:
        if len(valid) >= wanted:
            break
        idx = [i for i in dict.fromkeys(idx) if 0 <= i < n]
        if len(idx) >= 2 and not any(v.scene_indices == idx for v in valid):
            valid.append(DraftShort(title=title[:95], scene_indices=idx))
    return valid[:wanted]


def _dedupe_lower(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out
