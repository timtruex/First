"""Render backend contract.

The orchestrator talks only to FrameSource. The web backend below is the one that
runs in this container (Chromium + ffmpeg, no system graphics deps); a Manim backend
can be dropped in on a host with cairo/pango without touching any calling code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence


class FrameSource(Protocol):
    fps: int
    width: int
    height: int

    def duration_seconds(self) -> float: ...

    def render_frames(self, out_dir: Path) -> Sequence[Path]:
        """Render every frame to out_dir, returning the paths in presentation order."""
        ...

    def render_probe(self, out_dir: Path) -> dict[str, Path]:
        """Render one representative frame per beat, keyed by beat id."""
        ...
