"""1280x720 thumbnail built from the hero photo plus the script's thumbnail text."""
from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from ..render.cards import ACCENT, INK, PAPER, Fonts

SIZE = (1280, 720)


def build_thumbnail(hero: Path, text: str, fonts: Fonts, dest: Path) -> Path:
    """Product right, text left. A darkened gradient keeps the text readable over any photo."""
    with Image.open(hero) as raw:
        photo = raw.convert("RGB")

    backdrop = _cover(photo, SIZE).filter(ImageFilter.GaussianBlur(28))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(0.45)

    inset_w = round(SIZE[0] * 0.52)
    scale = min(inset_w / photo.width, SIZE[1] * 0.94 / photo.height)
    inset = photo.resize((max(1, round(photo.width * scale)), max(1, round(photo.height * scale))), Image.LANCZOS)
    backdrop.paste(inset, (SIZE[0] - inset.width - 24, (SIZE[1] - inset.height) // 2))

    canvas = backdrop.convert("RGBA")
    scrim = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rectangle((0, 0, round(SIZE[0] * 0.56), SIZE[1]), fill=(8, 8, 8, 190))
    canvas = Image.alpha_composite(canvas, scrim)

    draw = ImageDraw.Draw(canvas)
    body = (text or "WORTH IT?").strip().upper()
    wrapped = textwrap.fill(body, width=11)
    font = _fit(draw, wrapped, fonts, max_width=round(SIZE[0] * 0.46), max_height=round(SIZE[1] * 0.6))

    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=8)
    x, y = 56, (SIZE[1] - (bbox[3] - bbox[1])) // 2
    draw.rectangle((0, y - 24, 16, y + (bbox[3] - bbox[1]) + 24), fill=ACCENT)
    draw.multiline_text(
        (x - bbox[0], y - bbox[1]),
        wrapped,
        font=font,
        fill=PAPER,
        spacing=8,
        stroke_width=4,
        stroke_fill=INK,
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(dest, "JPEG", quality=90, optimize=True)
    return dest


def _fit(draw, text: str, fonts: Fonts, *, max_width: int, max_height: int):
    for size in range(128, 40, -4):
        font = fonts.at(size)
        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=8)
        if (bbox[2] - bbox[0]) <= max_width and (bbox[3] - bbox[1]) <= max_height:
            return font
    return fonts.at(40)


def _cover(image: Image.Image, target: tuple[int, int]) -> Image.Image:
    tw, th = target
    scale = max(tw / image.width, th / image.height)
    resized = image.resize((max(tw, round(image.width * scale)), max(th, round(image.height * scale))), Image.LANCZOS)
    left = (resized.width - tw) // 2
    top = (resized.height - th) // 2
    return resized.crop((left, top, left + tw, top + th))
