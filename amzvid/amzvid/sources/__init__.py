"""Product asset sources. `manual` works today; `paapi` is a config flip once you have keys."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

import httpx
from PIL import Image, ImageOps

from ..config import Config
from ..models import Product, ProductImage

ASIN_RE = re.compile(r"(?:/dp/|/gp/product/|/gp/aw/d/|/product/|%2Fdp%2F|[?&]asin=)([A-Z0-9]{10})", re.I)
BARE_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
MAX_EDGE = 2400  # beyond this, zoompan prescaling costs more than it returns


class AssetSource(Protocol):
    name: str

    def fetch(self, affiliate_url: str, job_dir: Path) -> Product:
        """Return a Product with every image already on local disk."""
        ...


def extract_asin(url: str) -> str | None:
    """Pull the ASIN out of any Amazon URL shape, including SiteStripe and mobile links."""
    candidate = url.strip()
    if BARE_ASIN_RE.match(candidate.upper()):
        return candidate.upper()
    m = ASIN_RE.search(candidate)
    if m:
        return m.group(1).upper()
    # /Product-Name/dp/ASIN sometimes appears as a trailing path segment with no marker.
    for seg in reversed(candidate.split("?")[0].split("/")):
        if BARE_ASIN_RE.match(seg.upper()) and any(c.isdigit() for c in seg):
            return seg.upper()
    return None


def resolve_short_link(url: str, timeout: float = 15.0) -> str:
    """amzn.to links carry the tag but hide the ASIN. Follow redirects to recover it."""
    if "amzn.to" not in url and "a.co" not in url:
        return url
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            resp = client.head(url)
            if resp.status_code >= 400:
                resp = client.get(url)
            return str(resp.url)
    except httpx.HTTPError:
        return url


def ensure_tag(url: str, tag: str) -> str:
    """Guarantee the affiliate tag survives, whatever shape the pasted link had."""
    if not tag or f"tag={tag}" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}tag={tag}"


def canonical_url(asin: str, tag: str) -> str:
    return f"https://www.amazon.com/dp/{asin}?tag={tag}" if tag else f"https://www.amazon.com/dp/{asin}"


def normalize_image(src: Path, dest_dir: Path, index: int) -> ProductImage:
    """Flatten to RGB JPEG, honor EXIF rotation, cap the long edge, drop metadata."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"img_{index:02d}.jpg"
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", im.size, (255, 255, 255))
            converted = im.convert("RGBA")
            background.paste(converted, mask=converted.split()[-1])
            im = background
        else:
            im = im.convert("RGB")
        if max(im.size) > MAX_EDGE:
            im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        im.save(dest, "JPEG", quality=92, optimize=True)
        width, height = im.size
    return ProductImage(path=dest, width=width, height=height)


def download_images(urls: list[str], dest_dir: Path, *, timeout: float = 30.0) -> list[ProductImage]:
    """Fetch remote product images and normalize them onto local disk."""
    raw = dest_dir / "_raw"
    raw.mkdir(parents=True, exist_ok=True)
    out: list[ProductImage] = []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for i, url in enumerate(urls):
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            tmp = raw / f"src_{i:02d}"
            tmp.write_bytes(resp.content)
            try:
                out.append(normalize_image(tmp, dest_dir, i))
            except OSError:
                continue
    return out


def get_source(cfg: Config) -> AssetSource:
    if cfg.source == "paapi":
        from .paapi import PaapiSource

        return PaapiSource(cfg)
    from .manual import ManualSource

    return ManualSource(cfg)
