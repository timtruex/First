"""Tests for tools/bookmarklet.js, the browser capture script.

This is browser code, so it runs in jsdom through tests/capture_harness.js. It is also
the part of this project that has broken most often and most expensively - the defect
these tests exist for shipped three certification badges as the product photos of a
$499 device - so it gets real coverage rather than none.

Skipped when node or jsdom is absent; CI installs jsdom.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
HARNESS = ROOT / "capture_harness.js"


def _jsdom_available() -> bool:
    if not shutil.which("node"):
        return False
    probe = subprocess.run(
        ["node", "-e", "require('jsdom')"], cwd=ROOT.parent, capture_output=True
    )
    return probe.returncode == 0


_HAVE_JSDOM = _jsdom_available()

# These tests skip on a machine without jsdom, which is the right default locally and
# the wrong one in CI: a silent skip is how this path came to be uncovered. CI sets
# AMZVID_REQUIRE_JSDOM=1 so a missing or broken jsdom fails instead.
if not _HAVE_JSDOM and os.environ.get("AMZVID_REQUIRE_JSDOM") == "1":
    raise RuntimeError(
        "AMZVID_REQUIRE_JSDOM=1 but node/jsdom is unavailable, so the capture tests "
        "would have been skipped. Run `npm install --no-save jsdom`."
    )

pytestmark = pytest.mark.skipif(
    not _HAVE_JSDOM, reason="needs node with jsdom (npm install --no-save jsdom)"
)


def capture(fixture: str) -> dict:
    """Run the bookmarklet against a fixture and return what it emitted."""
    done = subprocess.run(
        ["node", str(HARNESS), str(FIXTURES / fixture)],
        cwd=ROOT.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, f"harness failed: {done.stderr}"
    payload = json.loads(done.stdout)
    assert "harness_error" not in payload, payload["harness_error"]
    return payload


def image_ids(payload: dict) -> list[str]:
    return [u.split("/I/")[1].split(".")[0] for u in payload.get("images", [])]


# --- the defect this file exists for -------------------------------------------------

#: Every one of these appeared as a "product photo" in a published video.
BADGES = ["31CarbonTrust", "31EnergyStar", "41TuvRheinland"]


def test_certification_badges_are_never_product_photos():
    ids = image_ids(capture("amazon_badges.html"))
    assert ids == ["71ProductFront", "61ProductAngle", "81ProductWall"]
    for badge in BADGES:
        assert badge not in ids


def test_aplus_and_recommendation_rails_are_excluded():
    """A+ marketing panels and 'similar items' carry the same /images/I/ path as the gallery."""
    ids = image_ids(capture("amazon_badges.html"))
    assert "41AplusLifestyle" not in ids
    assert "51OtherProduct" not in ids


def test_a_page_with_only_furniture_refuses_rather_than_shipping_it():
    """The live failure: the gallery was lazy-loaded, so badges were all that remained.

    Handing those back produced a review of a product that never appeared on screen.
    Refusing costs the user one more swipe; not refusing costs them the video.
    """
    payload = capture("amazon_badges_only.html")
    assert "images" not in payload or not payload["images"]
    assert "refused" in payload
    assert "scroll the photo gallery" in payload["refused"].lower()


def test_a_size_token_alone_does_not_admit_an_image():
    """Amazon upscales any image to 1500px on request, so a token proves nothing.

    41TuvRheinland is authored at ._AC_SL1500_ and rendered at 60px. It passed the old
    check on its token and arrived as a full-screen scene.
    """
    assert "41TuvRheinland" not in image_ids(capture("amazon_badges.html"))


# --- the cases earlier fixes were written for, so they cannot regress ----------------


@pytest.mark.parametrize(
    "fixture, least",
    [
        ("amazon_like.html", 3),
        ("amazon_no_colorimages.html", 1),
        ("amazon_mobile_like.html", 3),
        ("amazon_mobile_gallery_json.html", 3),
    ],
)
def test_known_pages_still_yield_product_photos(fixture, least):
    payload = capture(fixture)
    assert len(payload.get("images", [])) >= least, payload


def test_mobile_chrome_is_not_mistaken_for_the_gallery():
    payload = capture("amazon_mobile_chrome.html")
    ids = image_ids(payload)
    assert not any("sprite" in i.lower() or "nav" in i.lower() for i in ids), ids


def test_payload_shape_is_stable():
    payload = capture("amazon_like.html")
    assert payload["amzvid"] == 1
    assert payload["asin"]
    assert payload["url"].startswith("https://www.amazon.com/dp/")
    assert isinstance(payload["bullets"], list)
    assert isinstance(payload["images"], list)
