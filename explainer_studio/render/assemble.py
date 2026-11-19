"""Frame sequence -> H.264 master. Deterministic: no filters that read wall-clock."""
from __future__ import annotations

import subprocess
from pathlib import Path


def assemble(frames_dir: Path, out: Path, fps: int = 30, audio: Path | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-framerate", str(fps),
        "-i", str(frames_dir / "f%06d.png"),
    ]
    if audio and audio.exists():
        cmd += ["-i", str(audio), "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += [
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


def shorts_cutdown(master: Path, out: Path, start: float, duration: float = 45.0) -> Path:
    """Vertical 1080x1920 cut of the single most satisfying reveal.

    One render, two products: the same master feeds long-form and the Shorts funnel,
    so top-of-funnel costs an ffmpeg pass rather than a second production.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start), "-t", str(duration), "-i", str(master),
        "-vf", "scale=1080:-2,pad=1080:1920:0:(oh-ih)/2:color=#11161d",
        "-c:v", "libx264", "-preset", "slow", "-crf", "19",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
    ], check=True, capture_output=True, text=True)
    return out
