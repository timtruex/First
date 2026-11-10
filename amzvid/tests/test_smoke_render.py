"""End-to-end render smoke test.

Exercises the real ffmpeg path - plates, Ken Burns motion, the frame-aligned audio grid,
caption burn-in, overlay compositing, cross-fade assembly and the Shorts cuts - using
dry-run stand-ins for the two paid APIs. This is the test that caught the defects a unit
test could not see, so it asserts on the actual output files rather than on return values.

Marked `slow` and deselected by default; CI runs it with `pytest -m slow`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed"),
    pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is not installed"),
]

FPS = 30
PHOTO_SPECS = [  # mixed aspects on purpose: forces both the cover and blur-pad paths
    (1600, 1600), (1800, 1200), (1200, 1600), (2000, 1125), (1400, 1400),
]


def _probe(path: Path, stream: str, fields: str) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream,
         "-show_entries", f"stream={fields}", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return out.strip().splitlines()


def _duration(path: Path, stream: str) -> float:
    return float(_probe(path, stream, "duration")[0])


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> tuple[Path, list]:
    from PIL import Image, ImageDraw

    root = Path(os.environ.get("AMZVID_SMOKE_DIR") or tmp_path_factory.mktemp("smoke"))
    photos = root / "photos"
    assets = root / "assets"
    for sub in (photos, assets / "fonts", assets / "music"):
        sub.mkdir(parents=True, exist_ok=True)

    for i, (w, h) in enumerate(PHOTO_SPECS):
        image = Image.new("RGB", (w, h), (210 + i * 5, 215, 225 - i * 4))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((w * 0.18, h * 0.18, w * 0.82, h * 0.82), radius=40, fill=(40, 44, 52))
        draw.ellipse((w * 0.36, h * 0.36, w * 0.64, h * 0.64), fill=(255, 215, 54))
        image.save(photos / f"p{i}.jpg", quality=92)

    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    ):
        if Path(candidate).exists():
            shutil.copy2(candidate, assets / "fonts" / Path(candidate).name)
            break
    else:
        pytest.skip("no usable font on this machine")

    config = root / "config.toml"
    config.write_text(f"""
[general]
affiliate_tag = "smoketest-20"
channel_name  = "Smoke Test Channel"
work_dir      = "{root / 'work'}"
output_dir    = "{root / 'out'}"
assets_dir    = "{assets}"
source        = "manual"
dry_run       = true
shorts_per_video = 3

[voice]
voice_id = "smoke"

[render]
encoder = "libx264"
caption_font = "DejaVu Sans"
jobs = 2
transition_s = 0.45
""")

    os.environ["ELEVENLABS_API_KEY"] = "dry-run-not-used"
    from amzvid.config import load_config
    from amzvid.pipeline import run_render_stage, run_script_stage

    cfg = load_config(config)
    job_id = "smoketest"
    job_dir = cfg.paths.job_dir(job_id)
    (job_dir / "inbox").mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(sorted(photos.iterdir())):
        shutil.copy2(src, job_dir / "inbox" / f"{i:02d}{src.suffix}")
    (job_dir / "product.json").write_text(json.dumps({
        "title": "Acme Widget Pro, 3-Pack, Stainless Steel - Blue",
        "price": "$29.99",
        "notes": "The clamp is the real selling point.",
        "bullets": ["Folds flat to one inch", "Stainless clamp holds twenty pounds"],
    }))

    url = "https://www.amazon.com/dp/B08N5WRWNW?tag=smoketest-20"
    product, draft = run_script_stage(cfg, job_id, url)
    results = run_render_stage(cfg, job_id, product, draft)
    return cfg.paths.out_dir(job_id), results


def test_produces_one_long_cut_and_three_shorts(rendered):
    _out, results = rendered
    assert len(results) == 4
    assert [r.kind for r in results].count("long") == 1
    assert [r.kind for r in results].count("short") == 3


def test_every_deliverable_exists_and_is_not_empty(rendered):
    _out, results = rendered
    for item in results:
        assert item.path.exists(), f"{item.path} missing"
        assert item.path.stat().st_size > 10_000, f"{item.path.name} is suspiciously small"


def test_canvas_geometry_matches_the_cut(rendered):
    _out, results = rendered
    for item in results:
        width, height = (int(v) for v in _probe(item.path, "v:0", "width,height"))
        expected = (1920, 1080) if item.kind == "long" else (1080, 1920)
        assert (width, height) == expected, f"{item.path.name} is {width}x{height}"


def test_frame_rate_is_exact(rendered):
    _out, results = rendered
    for item in results:
        assert _probe(item.path, "v:0", "r_frame_rate")[0] == f"{FPS}/1"


def test_audio_and_video_stay_within_one_frame(rendered):
    """The frame-aligned grid is the whole reason captions do not drift. Guard it."""
    _out, results = rendered
    for item in results:
        drift = abs(_duration(item.path, "v:0") - _duration(item.path, "a:0"))
        assert drift < 1 / FPS, f"{item.path.name} drifts {drift * 1000:.0f}ms"


def test_every_cut_carries_an_audio_stream(rendered):
    _out, results = rendered
    for item in results:
        assert _probe(item.path, "a:0", "codec_name") == ["aac"]


def test_thumbnail_is_youtube_sized(rendered):
    out, _results = rendered
    thumbnail = out / "thumbnail.jpg"
    assert thumbnail.exists()
    from PIL import Image

    with Image.open(thumbnail) as image:
        assert image.size == (1280, 720)


def test_description_leads_with_the_disclosure_and_keeps_the_tag(rendered):
    from amzvid.publish.metadata import DISCLOSURE

    out, _results = rendered
    text = (out / "long.description.txt").read_text()
    assert DISCLOSURE in text
    assert "tag=smoketest-20" in text
    assert text.index(DISCLOSURE) < text.index("What I liked")


def test_metadata_json_is_well_formed(rendered):
    out, _results = rendered
    meta = json.loads((out / "long.metadata.json").read_text())
    assert meta["asin"] == "B08N5WRWNW"
    assert meta["duration_s"] > 10
    assert meta["tags"]
    assert "tag=smoketest-20" in meta["affiliate_url"]


def test_shorts_get_their_own_descriptions(rendered):
    out, _results = rendered
    for n in (1, 2, 3):
        text = (out / f"short{n}.description.txt").read_text()
        assert "#shorts" in text
        assert "tag=smoketest-20" in text
