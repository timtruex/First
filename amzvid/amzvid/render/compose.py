"""Build the still "plate" each scene's motion is applied to.

Composition happens once per scene with Pillow rather than inside the filtergraph: a
blurred-backdrop treatment costs one image operation here, but would cost one gaussian
blur per output frame if ffmpeg did it under -loop 1.

Two fit modes. `cover` crops to fill, which is right when the photo and the canvas are
close in aspect. `blurpad` contains the photo over a blurred, darkened copy of itself,
which is right when they are not - cropping a square product photo to 16:9 cuts the top
and bottom off the product, which is exactly the thing the viewer came to see.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

from ..config import Canvas

PRESCALE = 2  # zoom headroom; zoompan samples down from this, so motion stays sharp
BLURPAD_THRESHOLD = 0.30  # |ln(image aspect / canvas aspect)|; ~35% mismatch
BLUR_SIGMA_RATIO = 0.035  # blur radius as a fraction of plate width
BACKDROP_DIM = 0.55


def plate_path(work_dir: Path, canvas: Canvas, index: int) -> Path:
    return work_dir / f"plate_{canvas.name}_{index:02d}.jpg"


def choose_fit(image_aspect: float, canvas: Canvas) -> str:
    if image_aspect <= 0:
        return "cover"
    return "cover" if abs(math.log(image_aspect / canvas.aspect)) <= BLURPAD_THRESHOLD else "blurpad"


def build_plate(src: Path, dest: Path, canvas: Canvas, *, fit: str | None = None) -> Path:
    """Render one scene's still at prescale resolution. Idempotent: skips existing output."""
    target = (canvas.width * PRESCALE, canvas.height * PRESCALE)
    with Image.open(src) as raw:
        image = raw.convert("RGB")
        mode = fit or choose_fit(image.width / image.height if image.height else 1.0, canvas)
        plate = _cover(image, target) if mode == "cover" else _blurpad(image, target)

    dest.parent.mkdir(parents=True, exist_ok=True)
    plate.save(dest, "JPEG", quality=95, subsampling=0)
    return dest


def _cover(image: Image.Image, target: tuple[int, int]) -> Image.Image:
    tw, th = target
    scale = max(tw / image.width, th / image.height)
    resized = image.resize(
        (max(tw, round(image.width * scale)), max(th, round(image.height * scale))), Image.LANCZOS
    )
    left = (resized.width - tw) // 2
    top = (resized.height - th) // 2
    return resized.crop((left, top, left + tw, top + th))


def _blurpad(image: Image.Image, target: tuple[int, int]) -> Image.Image:
    tw, th = target
    backdrop = _cover(image, target).filter(
        ImageFilter.GaussianBlur(radius=max(8.0, tw * BLUR_SIGMA_RATIO))
    )
    backdrop = ImageEnhance.Brightness(backdrop).enhance(BACKDROP_DIM)

    scale = min(tw / image.width, th / image.height)
    fw = max(1, round(image.width * scale))
    fh = max(1, round(image.height * scale))
    foreground = image.resize((fw, fh), Image.LANCZOS)

    backdrop.paste(foreground, ((tw - fw) // 2, (th - fh) // 2))
    return backdrop
