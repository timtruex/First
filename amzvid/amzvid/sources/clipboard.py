"""Ingest a capture payload produced by the browser bookmarklet.

The bookmarklet runs in the operator's own browser on a page they are already viewing,
so this path makes no automated request to Amazon: it receives a JSON blob the same way
it would receive a paste. Images are then fetched from the CDN by URL.

The payload is untrusted input - it arrives via the clipboard or a Telegram message - so
every field is validated and clamped before it reaches the job.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import Config
from . import BARE_ASIN_RE, download_images

MAX_BULLETS = 12
MAX_IMAGES = 10
MAX_FIELD = 600
IMAGE_HOST_RE = re.compile(r"^https://[\w.-]*(?:media-amazon|ssl-images-amazon)\.com/", re.I)


class PayloadError(ValueError):
    """The blob is not a usable amzvid capture."""


def looks_like_payload(text: str) -> bool:
    """Cheap test so the Telegram bot can tell a capture from an ordinary message."""
    stripped = text.strip()
    return stripped.startswith("{") and '"amzvid"' in stripped


def parse_payload(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"that is not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict) or data.get("amzvid") != 1:
        raise PayloadError("that JSON is not an amzvid capture (missing \"amzvid\": 1)")

    # The Shortcut reports failures as a payload, because Shortcuts hangs if the
    # script returns nothing. Surface that message instead of a confusing ASIN error.
    if error := str(data.get("error", "")).strip():
        raise PayloadError(f"the capture failed on the page: {error}")

    asin = str(data.get("asin", "")).strip().upper()
    if not BARE_ASIN_RE.match(asin):
        raise PayloadError(f"no usable ASIN in the capture (got {asin!r})")

    images = [u for u in _strings(data.get("images"), MAX_IMAGES) if IMAGE_HOST_RE.match(u)]
    if not images:
        raise PayloadError(
            "the capture carried no Amazon image URLs. Scroll the gallery once so the "
            "photos load, then click the bookmarklet again."
        )

    return {
        "asin": asin,
        "url": f"https://www.amazon.com/dp/{asin}",
        "title": _clip(data.get("title")) or f"Amazon product {asin}",
        "brand": _clip(data.get("brand")),
        "price": _clip(data.get("price"), 40),
        "rating": _clip(data.get("rating"), 20),
        "review_count": _clip(data.get("review_count"), 40),
        "bullets": _strings(data.get("bullets"), MAX_BULLETS),
        "images": images,
    }


def ingest(cfg: Config, payload: dict[str, Any], job_dir: Path, notes: str = "") -> int:
    """Write product.json and pull the images into the job's inbox. Returns the count."""
    job_dir.mkdir(parents=True, exist_ok=True)
    inbox = job_dir / "inbox"
    inbox.mkdir(exist_ok=True)

    # normalize_image names these img_NN.jpg, and the manual source reads the inbox in
    # name order, so gallery order is preserved and image 0 stays the hero shot.
    fetched = download_images(payload["images"], inbox)

    (job_dir / "product.json").write_text(
        json.dumps(
            {
                "title": payload["title"],
                "price": payload["price"],
                "rating": payload["rating"],
                "review_count": payload["review_count"],
                "brand": payload["brand"],
                "bullets": payload["bullets"],
                "notes": notes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return len(fetched)


def _clip(value: Any, limit: int = MAX_FIELD) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clip(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) >= limit:
            break
    return out
