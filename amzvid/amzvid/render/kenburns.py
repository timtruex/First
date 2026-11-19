"""zoompan expressions for Ken Burns motion.

Every expression is driven by `on` (the output frame index) over a known frame count
rather than by accumulation (`z='zoom+0.001'`). Accumulating expressions drift with frame
rate and round-trip differently on a re-render; a closed form over on/N is deterministic,
so the same scene always produces the same motion.

The crop window is iw/z by ih/z, so x is valid on [0, iw - iw/z]. Panning expressions are
written against that range, which keeps the window inside the plate at every zoom level.
"""
from __future__ import annotations

from ..config import Canvas
from ..models import Move

ZOOM_RANGE = 0.14  # 14% travel reads as motion without looking like a slideshow effect
PAN_ZOOM = 1.14    # a pan needs standing zoom, otherwise there is nothing to pan across

CYCLE: tuple[Move, ...] = (
    "zoom_in",
    "pan_right",
    "zoom_out",
    "pan_left",
    "zoom_in_pan_right",
    "pan_up",
    "zoom_in_pan_left",
    "pan_down",
)


def pick_moves(count: int, *, start: int = 0) -> list[Move]:
    """Walk the cycle so adjacent scenes never repeat a move."""
    return [CYCLE[(start + i) % len(CYCLE)] for i in range(count)]


def frame_count(duration: float, canvas: Canvas) -> int:
    return max(2, round(duration * canvas.fps))


def zoompan_filter(move: Move, duration: float, canvas: Canvas) -> str:
    n = frame_count(duration, canvas)
    p = f"(on/{n - 1})"  # normalized progress, 0 -> 1

    match move:
        case "zoom_in":
            z = f"(1+{ZOOM_RANGE}*{p})"
            x, y = _centered(z)
        case "zoom_out":
            z = f"({1 + ZOOM_RANGE}-{ZOOM_RANGE}*{p})"
            x, y = _centered(z)
        case "pan_right":
            z = f"{PAN_ZOOM}"
            x, y = f"((iw-iw/{z})*{p})", f"((ih-ih/{z})/2)"
        case "pan_left":
            z = f"{PAN_ZOOM}"
            x, y = f"((iw-iw/{z})*(1-{p}))", f"((ih-ih/{z})/2)"
        case "pan_down":
            z = f"{PAN_ZOOM}"
            x, y = f"((iw-iw/{z})/2)", f"((ih-ih/{z})*{p})"
        case "pan_up":
            z = f"{PAN_ZOOM}"
            x, y = f"((iw-iw/{z})/2)", f"((ih-ih/{z})*(1-{p}))"
        case "zoom_in_pan_right":
            z = f"(1+{ZOOM_RANGE}*{p})"
            x, y = f"((iw-iw/{z})*{p})", f"((ih-ih/{z})/2)"
        case "zoom_in_pan_left":
            z = f"(1+{ZOOM_RANGE}*{p})"
            x, y = f"((iw-iw/{z})*(1-{p}))", f"((ih-ih/{z})/2)"
        case "static":
            z, (x, y) = "1", ("0", "0")
        case _:
            raise ValueError(f"unknown move: {move}")

    return (
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={canvas.width}x{canvas.height}:fps={canvas.fps}"
    )


def _centered(z: str) -> tuple[str, str]:
    return f"((iw-iw/{z})/2)", f"((ih-ih/{z})/2)"
