"""Overlay graphics, drawn with Pillow as full-canvas transparent PNGs.

Full-canvas means ffmpeg composites them at 0:0 and all positioning math lives here in
Python, where it can be read, rather than inside a filtergraph expression. These sit above
the Ken Burns motion in screen space, so they stay still while the photo moves.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import Canvas

ACCENT = (255, 215, 54, 255)
INK = (16, 16, 16, 255)
PAPER = (255, 255, 255, 255)
PANEL = (16, 16, 16, 225)
PRO = (74, 222, 128, 255)
CON = (248, 113, 113, 255)

FONT_CANDIDATES_BOLD = (
    "Montserrat-ExtraBold.ttf",
    "Inter-Bold.ttf",
    "Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
FONT_CANDIDATES_REGULAR = (
    "Montserrat-SemiBold.ttf",
    "Inter-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


class FontMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class Fonts:
    bold: Path
    regular: Path

    def at(self, size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(self.bold if bold else self.regular), size)


def resolve_fonts(assets_dir: Path) -> Fonts:
    bold = _first_font(assets_dir, FONT_CANDIDATES_BOLD)
    regular = _first_font(assets_dir, FONT_CANDIDATES_REGULAR) or bold
    if bold is None:
        raise FontMissing(
            f"no usable bold font found. Drop a .ttf into {assets_dir / 'fonts'} "
            "(Montserrat ExtraBold is a good default) and try again."
        )
    return Fonts(bold=bold, regular=regular)


def _first_font(assets_dir: Path, candidates: tuple[str, ...]) -> Path | None:
    fonts_dir = assets_dir / "fonts"
    for name in candidates:
        path = Path(name)
        for probe in ((fonts_dir / name) if not path.is_absolute() else path,):
            if probe.exists():
                return probe
    if fonts_dir.exists():
        for found in sorted(fonts_dir.glob("*.tt[fc]")):
            return found
    return None


def _blank(canvas: Canvas) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (canvas.width, canvas.height), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image)


def _scale(canvas: Canvas) -> float:
    return canvas.height / 1080 if canvas.width > canvas.height else canvas.width / 1080


# --------------------------------------------------------------------------- #

def lower_third(text: str, canvas: Canvas, fonts: Fonts, dest: Path) -> Path:
    """Top-left pill naming what is on screen. Kept clear of the bottom caption band."""
    image, draw = _blank(canvas)
    s = _scale(canvas)
    font = fonts.at(round(44 * s))
    pad_x, pad_y = round(32 * s), round(20 * s)
    margin = round(70 * s)

    label = text.strip().upper()
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    box = (margin, margin, margin + tw + pad_x * 2, margin + th + pad_y * 2)

    draw.rounded_rectangle(box, radius=round(14 * s), fill=PANEL)
    draw.rectangle((box[0], box[1], box[0] + round(8 * s), box[3]), fill=ACCENT)
    draw.text((box[0] + pad_x + round(8 * s), box[1] + pad_y - bbox[1]), label, font=font, fill=PAPER)

    _save(image, dest)
    return dest


def price_badge(price: str, canvas: Canvas, fonts: Fonts, dest: Path) -> Path:
    """Top-right price. On screen only, never spoken, because the number goes stale."""
    image, draw = _blank(canvas)
    s = _scale(canvas)
    big = fonts.at(round(56 * s))
    small = fonts.at(round(24 * s), bold=False)
    margin = round(70 * s)
    pad = round(26 * s)

    caption = "PRICE AT RECORDING"
    cb = draw.textbbox((0, 0), caption, font=small)
    pb = draw.textbbox((0, 0), price, font=big)
    width = max(cb[2] - cb[0], pb[2] - pb[0]) + pad * 2
    height = (cb[3] - cb[1]) + (pb[3] - pb[1]) + pad * 2 + round(10 * s)

    right = canvas.width - margin
    box = (right - width, margin, right, margin + height)
    draw.rounded_rectangle(box, radius=round(16 * s), fill=ACCENT)
    draw.text((box[0] + pad, box[1] + pad - cb[1]), caption, font=small, fill=INK)
    draw.text(
        (box[0] + pad, box[1] + pad + (cb[3] - cb[1]) + round(10 * s) - pb[1]),
        price,
        font=big,
        fill=INK,
    )

    _save(image, dest)
    return dest


def pros_cons(pros: list[str], cons: list[str], canvas: Canvas, fonts: Fonts, dest: Path) -> Path:
    """The honest-drawback card. Earns trust and holds attention through the con scene."""
    image, draw = _blank(canvas)
    s = _scale(canvas)
    heading = fonts.at(round(34 * s))
    body = fonts.at(round(38 * s))
    line_h = round(62 * s)
    pad = round(44 * s)

    rows = [("GOOD", PRO, pros), ("NOT SO GOOD", CON, cons)]
    total = sum(round(46 * s) + line_h * len(items) + pad // 2 for _, _, items in rows) + pad
    width = round((canvas.width * (0.46 if canvas.width > canvas.height else 0.86)))
    left = canvas.width - width - round(80 * s) if canvas.width > canvas.height else (canvas.width - width) // 2
    top = (canvas.height - total) // 2

    draw.rounded_rectangle((left, top, left + width, top + total), radius=round(22 * s), fill=PANEL)

    y = top + pad // 2
    for title, colour, items in rows:
        if not items:
            continue
        draw.text((left + pad, y), title, font=heading, fill=colour)
        y += round(46 * s)
        for item in items:
            draw.ellipse(
                (left + pad, y + round(16 * s), left + pad + round(14 * s), y + round(30 * s)),
                fill=colour,
            )
            draw.text((left + pad + round(32 * s), y), item, font=body, fill=PAPER)
            y += line_h
        y += pad // 2

    _save(image, dest)
    return dest


def title_card(text: str, canvas: Canvas, fonts: Fonts, dest: Path) -> Path:
    """Opening frame for a Short: large text over the first photo."""
    image, draw = _blank(canvas)
    s = _scale(canvas)
    font = fonts.at(round(84 * s))
    wrapped = textwrap.fill(text.strip().upper(), width=16 if canvas.height > canvas.width else 26)

    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=round(14 * s), align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (canvas.width - tw) // 2
    y = round(canvas.height * (0.14 if canvas.height > canvas.width else 0.12))

    pad = round(34 * s)
    draw.rounded_rectangle(
        (x - pad, y - pad, x + tw + pad, y + th + pad), radius=round(20 * s), fill=PANEL
    )
    draw.multiline_text(
        (x - bbox[0], y - bbox[1]),
        wrapped,
        font=font,
        fill=PAPER,
        spacing=round(14 * s),
        align="center",
        stroke_width=round(3 * s),
        stroke_fill=INK,
    )

    _save(image, dest)
    return dest


def _save(image: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(dest, "PNG")
