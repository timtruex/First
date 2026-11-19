"""Episode state machine.

The orchestrator -- not an LLM -- owns control flow. Each stage is idempotent and
resumable, so a failure re-enters at the stage boundary rather than re-running the
episode. Sub-agents are called for judgment work only; rendering, assembly, QC and
upload are deterministic code.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.ledger import Ledger, Stage  # noqa: E402
from qc.gates import run_all  # noqa: E402
from render.assemble import assemble, shorts_cutdown  # noqa: E402
from render.web_backend import WebFrameSource  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def run(slug: str, build_root: Path, *, skip_render: bool = False) -> int:
    ep = ROOT / "episodes" / slug
    build = build_root / slug
    ledger = Ledger(build_root / "ledger.db")
    import yaml

    script = yaml.safe_load((ep / "script.yaml").read_text())
    ledger.ensure_episode(slug, script["title"])

    source = WebFrameSource(
        ROOT / "render" / "scenes" / f"{slug.replace('-', '_')}.html", ep / "script.yaml"
    )
    frames, master = build / "frames", build / f"{slug}.mp4"

    if not skip_render:
        source.render_frames(frames)
        ledger.record(slug, Stage.RENDER, "ok", f"{len(list(frames.glob('f*.png')))} frames",
                      str(frames))

    assemble(frames, master, fps=source.fps)
    ledger.record(slug, Stage.ASSEMBLE, "ok", f"{master.stat().st_size/1e6:.1f} MB", str(master))

    results = run_all(ep, master)
    blocking = [r for r in results if not r.passed and r.blocking]
    for r in results:
        print(f"{r.status:5} {r.name:18} {r.detail}")
        for f in r.findings:
            print("        -", f)
    ledger.record(slug, Stage.QC, "fail" if blocking else "ok",
                  "; ".join(f"{r.name}={r.status}" for r in results))
    if blocking:
        print("\nBLOCKED: QC gate failure. Not publishing.")
        return 1

    short = shorts_cutdown(master, build / f"{slug}-short.mp4", start=24.0, duration=45.0)
    print(f"\nmaster: {master}\nshort:  {short}")
    print("Next: publish/youtube.py uploads PRIVATE; flipping to public stays a human action.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--build", default=str(ROOT.parent / "build"))
    ap.add_argument("--skip-render", action="store_true")
    a = ap.parse_args()
    raise SystemExit(run(a.slug, Path(a.build), skip_render=a.skip_render))
