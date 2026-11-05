"""ASS subtitle generation with per-word highlighting.

One Dialogue event per word, each rendering the whole caption line with the spoken word
recolored. This is what separates these videos from block-level captions that drift: the
highlight lands on the word because the word's start and end came from the TTS alignment.

Only the colour changes between events - no glyph scaling - so the line never reflows and
the text does not jitter under the highlight.
"""
from __future__ import annotations

from pathlib import Path

from ..config import Canvas, RenderConfig
from ..models import Word
from ..voice.timing import chunk_words

HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},{primary},{highlight},{outline},&H90000000,-1,0,0,0,100,100,0,0,1,{border},{shadow},2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centis = int(round(seconds * 100))
    hours, rem = divmod(centis, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def escape_text(text: str) -> str:
    """Braces open override blocks in ASS; a literal brace from a product name must not."""
    return (
        text.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", "\\N")
    )


def build_ass(words: list[Word], canvas: Canvas, cfg: RenderConfig) -> str:
    size = cfg.caption_size_short if canvas.height > canvas.width else cfg.caption_size_long
    scale = canvas.height / 1080
    out = [
        HEADER.format(
            width=canvas.width,
            height=canvas.height,
            font=cfg.caption_font,
            size=size,
            primary=cfg.primary_color,
            highlight=cfg.highlight_color,
            outline=cfg.outline_color,
            border=round(4 * scale, 1),
            shadow=round(2 * scale, 1),
            margin_h=round(100 * scale),
            margin_v=round((320 if canvas.height > canvas.width else 110) * scale),
        )
    ]

    for chunk in chunk_words(words, cfg.caption_words_per_chunk):
        pieces = [escape_text(w.text) for w in chunk]
        for i, word in enumerate(chunk):
            # Hold the last word of a line until the next line starts, so the caption
            # never blinks out during a pause.
            end = chunk[i + 1].start if i + 1 < len(chunk) else word.end
            rendered = " ".join(
                f"{{\\c{cfg.highlight_color}}}{piece}{{\\c{cfg.primary_color}}}"
                if j == i
                else piece
                for j, piece in enumerate(pieces)
            )
            out.append(
                f"Dialogue: 0,{ass_time(word.start)},{ass_time(max(end, word.start + 0.05))},"
                f"Caption,,0,0,0,,{{\\c{cfg.primary_color}}}{rendered}"
            )

    return "\n".join(out) + "\n"


def write_ass(path: Path, words: list[Word], canvas: Canvas, cfg: RenderConfig) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_ass(words, canvas, cfg), encoding="utf-8")
    return path
