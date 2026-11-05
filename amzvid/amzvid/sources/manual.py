"""Manual asset source: you supply the images and (optionally) the product copy.

Zero dependency on Amazon's anti-bot posture, and the photos are the ones you chose
rather than whatever happens to be image #1 in the listing.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..config import Config
from ..models import Product
from . import canonical_url, ensure_tag, extract_asin, normalize_image, resolve_short_link

DROP_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tiff"}


class ManualSource:
    name = "manual"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def fetch(self, affiliate_url: str, job_dir: Path) -> Product:
        resolved = resolve_short_link(affiliate_url)
        asin = extract_asin(resolved) or extract_asin(affiliate_url) or "UNKNOWN"
        tag = self.cfg.affiliate_tag
        url = ensure_tag(affiliate_url, tag) if "tag=" in affiliate_url or not tag else ensure_tag(
            canonical_url(asin, tag), tag
        )

        meta = self._read_meta(job_dir)
        images = self._collect_images(job_dir)
        if not images:
            raise FileNotFoundError(
                f"no images found in {job_dir / 'inbox'} - send at least 2 product photos "
                "before running the job"
            )

        return Product(
            asin=asin,
            title=meta.get("title") or f"Amazon product {asin}",
            affiliate_url=url,
            images=images,
            bullets=[b for b in meta.get("bullets", []) if b.strip()],
            brand=meta.get("brand", ""),
            price=meta.get("price", ""),
            rating=meta.get("rating", ""),
            review_count=meta.get("review_count", ""),
            researcher_notes=meta.get("notes", ""),
            source=self.name,
        )

    def _read_meta(self, job_dir: Path) -> dict:
        path = job_dir / "product.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def _collect_images(self, job_dir: Path):
        inbox = job_dir / "inbox"
        assets = job_dir / "assets"
        if not inbox.exists():
            return []
        files = sorted(p for p in inbox.iterdir() if p.suffix.lower() in DROP_SUFFIXES)
        out = []
        for i, src in enumerate(files):
            try:
                out.append(normalize_image(src, assets, i))
            except OSError:
                shutil.move(str(src), str(src.with_suffix(src.suffix + ".unreadable")))
        return out
