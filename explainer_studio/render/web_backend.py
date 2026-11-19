"""Deterministic SVG frame renderer (Node Playwright + Chromium).

Python owns the timeline so that script.yaml stays the single source of truth for
pacing; Node owns only the pixel loop. Frame N is a pure function of N, which is what
makes an episode reproducible from script + dossier alone.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import yaml

HERE = Path(__file__).resolve().parent
RENDERER = HERE / "render_frames.js"


@dataclass(slots=True)
class WebFrameSource:
    scene_html: Path
    script_yaml: Path
    fps: int = 30
    width: int = 1920
    height: int = 1080

    def beats(self) -> list[dict]:
        return yaml.safe_load(self.script_yaml.read_text())["beats"]

    def duration_seconds(self) -> float:
        return float(sum(b["seconds"] for b in self.beats()))

    def _timeline(self, build: Path) -> Path:
        build.mkdir(parents=True, exist_ok=True)
        p = build / "timeline.json"
        p.write_text(json.dumps(self.beats(), indent=2))
        return p

    def _run(self, out_dir: Path, probe: bool) -> dict:
        build = out_dir.parent
        cmd = [
            "node", str(RENDERER),
            "--scene", str(self.scene_html.resolve()),
            "--timeline", str(self._timeline(build)),
            "--out", str(out_dir),
            "--fps", str(self.fps),
            "--width", str(self.width),
            "--height", str(self.height),
        ]
        if probe:
            cmd.append("--probe")
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout.strip().splitlines()[-1])

    def render_frames(self, out_dir: Path) -> Sequence[Path]:
        self._run(out_dir, probe=False)
        return sorted(out_dir.glob("f*.png"))

    def render_probe(self, out_dir: Path) -> dict[str, Path]:
        self._run(out_dir, probe=True)
        return {p.stem.removeprefix("probe_"): p for p in sorted(out_dir.glob("probe_*.png"))}
