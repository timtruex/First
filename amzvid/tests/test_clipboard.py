"""Tests for the bookmarklet capture path.

The payload arrives from a clipboard or a Telegram message, so it is untrusted input:
these cover the malformed and hostile shapes as well as the happy one.
"""
from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from amzvid.sources.clipboard import (
    MAX_BULLETS,
    MAX_IMAGES,
    PayloadError,
    ingest,
    looks_like_payload,
    parse_payload,
)

IMG = "https://m.media-amazon.com/images/I/{}._AC_SL1500_.jpg"


def payload(**overrides) -> str:
    body = {
        "amzvid": 1,
        "asin": "B08N5WRWNW",
        "title": "  Acme Widget Pro,  3-Pack  ",
        "brand": "Acme",
        "price": "$29.99",
        "rating": "4.4",
        "review_count": "1,284 ratings",
        "bullets": ["Folds flat", "Holds twenty pounds"],
        "images": [IMG.format("aaa"), IMG.format("bbb")],
    }
    body.update(overrides)
    return json.dumps(body)


# --------------------------------------------------------------------------- #

def test_looks_like_payload_accepts_a_capture():
    assert looks_like_payload(payload())
    assert looks_like_payload("  " + payload() + "\n")


def test_looks_like_payload_rejects_ordinary_messages():
    assert not looks_like_payload("great clamp, tested for two weeks")
    assert not looks_like_payload("https://www.amazon.com/dp/B08N5WRWNW")
    assert not looks_like_payload('{"something": "else"}')


def test_parses_a_good_capture():
    data = parse_payload(payload())
    assert data["asin"] == "B08N5WRWNW"
    assert data["title"] == "Acme Widget Pro, 3-Pack"  # whitespace collapsed
    assert data["url"] == "https://www.amazon.com/dp/B08N5WRWNW"
    assert len(data["images"]) == 2


def test_rejects_invalid_json():
    with pytest.raises(PayloadError, match="valid JSON"):
        parse_payload("{not json")


def test_rejects_json_that_is_not_a_capture():
    with pytest.raises(PayloadError, match="amzvid"):
        parse_payload('{"asin": "B08N5WRWNW"}')


def test_rejects_a_list():
    with pytest.raises(PayloadError):
        parse_payload('[1, 2, 3]')


def test_rejects_a_bad_asin():
    with pytest.raises(PayloadError, match="ASIN"):
        parse_payload(payload(asin="nope"))


def test_rejects_a_capture_with_no_images():
    with pytest.raises(PayloadError, match="image"):
        parse_payload(payload(images=[]))


def test_drops_image_urls_from_other_hosts():
    """A capture is untrusted; it must not make the bot fetch arbitrary URLs."""
    with pytest.raises(PayloadError, match="image"):
        parse_payload(payload(images=["https://evil.example.com/x.jpg", "http://127.0.0.1/y.jpg"]))


def test_keeps_only_the_amazon_urls_when_mixed():
    data = parse_payload(payload(images=["https://evil.example.com/x.jpg", IMG.format("aaa")]))
    assert data["images"] == [IMG.format("aaa")]


def test_image_urls_must_be_https():
    with pytest.raises(PayloadError):
        parse_payload(payload(images=["http://m.media-amazon.com/images/I/aaa.jpg"]))


def test_bullets_are_deduped_and_capped():
    many = [f"bullet {i}" for i in range(30)] + ["bullet 0"]
    data = parse_payload(payload(bullets=many))
    assert len(data["bullets"]) == MAX_BULLETS
    assert len(set(data["bullets"])) == len(data["bullets"])


def test_images_are_capped():
    data = parse_payload(payload(images=[IMG.format(f"i{n}") for n in range(40)]))
    assert len(data["images"]) == MAX_IMAGES


def test_long_fields_are_clipped():
    data = parse_payload(payload(title="x" * 5000, price="y" * 500))
    assert len(data["title"]) <= 600
    assert len(data["price"]) <= 40


def test_missing_title_falls_back_to_the_asin():
    assert parse_payload(payload(title=""))["title"] == "Amazon product B08N5WRWNW"


def test_non_list_bullets_are_ignored():
    assert parse_payload(payload(bullets="not a list"))["bullets"] == []


# --------------------------------------------------------------------------- #

@pytest.fixture
def image_server(tmp_path):
    """Serve two real JPEGs so ingest exercises the download and normalize path."""
    from PIL import Image

    root = tmp_path / "srv"
    root.mkdir()
    for name, size in (("a.jpg", (900, 900)), ("b.jpg", (1200, 800))):
        Image.new("RGB", size, (200, 180, 160)).save(root / name)

    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_ingest_downloads_images_and_writes_product_json(tmp_path, image_server, monkeypatch):
    from amzvid.config import load_config

    config = tmp_path / "c.toml"
    config.write_text(
        f'[general]\naffiliate_tag = "t-20"\nwork_dir = "{tmp_path / "w"}"\n'
        f'output_dir = "{tmp_path / "o"}"\n\n[voice]\nvoice_id = "v"\n'
    )
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    cfg = load_config(config)

    data = {
        "asin": "B08N5WRWNW",
        "title": "Acme Widget Pro",
        "brand": "Acme",
        "price": "$29.99",
        "rating": "4.4",
        "review_count": "1,284 ratings",
        "bullets": ["Folds flat", "Holds twenty pounds"],
        "images": [f"{image_server}/a.jpg", f"{image_server}/b.jpg"],
    }
    job_dir = tmp_path / "job"
    count = ingest(cfg, data, job_dir, notes="the clamp is the point")

    assert count == 2
    saved = sorted((job_dir / "inbox").glob("img_*.jpg"))
    assert len(saved) == 2

    meta = json.loads((job_dir / "product.json").read_text())
    assert meta["title"] == "Acme Widget Pro"
    assert meta["notes"] == "the clamp is the point"
    assert meta["bullets"] == ["Folds flat", "Holds twenty pounds"]


def test_ingest_survives_a_dead_image_url(tmp_path, image_server, monkeypatch):
    from amzvid.config import load_config

    config = tmp_path / "c.toml"
    config.write_text(
        f'[general]\nwork_dir = "{tmp_path / "w"}"\noutput_dir = "{tmp_path / "o"}"\n'
        f'\n[voice]\nvoice_id = "v"\n'
    )
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    cfg = load_config(config)

    data = {
        "asin": "B08N5WRWNW", "title": "t", "brand": "", "price": "", "rating": "",
        "review_count": "", "bullets": [],
        "images": [f"{image_server}/a.jpg", f"{image_server}/missing.jpg"],
    }
    assert ingest(cfg, data, tmp_path / "job2") == 1


# --------------------------------------------------------------------------- #

def test_built_bookmarklet_is_in_sync_with_its_source():
    """Editing bookmarklet.js without rebuilding would ship a stale install page."""
    import sys

    tools = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    try:
        import build_bookmarklet
    finally:
        sys.path.pop(0)

    expected = build_bookmarklet.build_url((tools / "bookmarklet.js").read_text())
    assert (tools / "bookmarklet.url.txt").read_text().strip() == expected, (
        "run: python3 tools/build_bookmarklet.py"
    )
    assert expected in (tools / "install.html").read_text().replace("&quot;", '"').replace("&amp;", "&")


def test_bookmarklet_url_is_a_javascript_url():
    tools = Path(__file__).resolve().parent.parent / "tools"
    url = (tools / "bookmarklet.url.txt").read_text().strip()
    assert url.startswith("javascript:")
    assert "\n" not in url


def test_preview_collapses_and_truncates():
    from amzvid.cli import _preview

    assert _preview("") == "(nothing)"
    assert _preview("  a\n\n  b  ") == "a b"
    assert _preview("x" * 300).endswith("...")
    assert len(_preview("x" * 300)) == 123


def test_read_capture_prefers_an_explicit_file(tmp_path):
    from amzvid.cli import _read_capture

    f = tmp_path / "cap.json"
    f.write_text('{"amzvid": 1}')
    text, origin = _read_capture(f)
    assert text == '{"amzvid": 1}'
    assert origin == str(f)


def test_read_clipboard_returns_empty_without_pbpaste(monkeypatch):
    """watch must degrade quietly off macOS rather than reading stdin."""
    import amzvid.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli._read_clipboard() == ""


def test_require_subtitles_names_the_fix(monkeypatch, tmp_path):
    """An ffmpeg without libass must fail before the render, with the remedy."""
    from amzvid.config import load_config
    from amzvid.render import ffmpeg as ff

    config = tmp_path / "c.toml"
    config.write_text(
        f'[general]\nwork_dir = "{tmp_path / "w"}"\noutput_dir = "{tmp_path / "o"}"\n'
        f'\n[voice]\nvoice_id = "v"\n'
    )
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    cfg = load_config(config)

    monkeypatch.setitem(ff._FILTER_CACHE, cfg.ffmpeg, {"scale", "overlay", "xfade"})
    with pytest.raises(ff.FFmpegError, match="libass"):
        ff.require_subtitles(cfg)

    monkeypatch.setitem(ff._FILTER_CACHE, cfg.ffmpeg, {"subtitles", "scale"})
    ff.require_subtitles(cfg)  # present: returns quietly


def test_capture_error_payload_is_surfaced(): 
    """The Shortcut reports failures as a payload; the message must reach the user."""
    blob = json.dumps({"amzvid": 1, "error": "found no images. Scroll the photo gallery once."})
    with pytest.raises(PayloadError, match="no images"):
        parse_payload(blob)


def test_shortcut_build_is_in_sync_and_calls_completion():
    """The Shortcut variant is generated; a stale copy would hang the Shortcut."""
    import sys

    tools = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    try:
        import build_bookmarklet
    finally:
        sys.path.pop(0)

    expected = build_bookmarklet.build_shortcut((tools / "bookmarklet.js").read_text())
    assert (tools / "shortcut.js").read_text() == expected, (
        "run: python3 tools/build_bookmarklet.py"
    )
    assert "completion(" in expected
    assert "navigator.clipboard" in expected  # copy() is still defined, just unused by emit


def test_every_bookmarklet_exit_goes_through_emit():
    """A path that returns without calling emit() hangs the Shortcut build."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "bookmarklet.js").read_text()
    body = source[source.index("var id = asin();"):]
    returns = body.count("return;")
    emits = body.count("emit(")
    assert emits >= returns, f"{returns} early returns but only {emits} emit() calls"


def test_bookmarklet_caps_the_payload_for_telegram():
    """Telegram rejects a sendMessage text over 4096 chars outright."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "bookmarklet.js").read_text()
    assert "MAX_PAYLOAD_CHARS" in source
    cap = int(source.split("MAX_PAYLOAD_CHARS = ")[1].split(";")[0])
    assert cap <= 4096, f"payload cap {cap} exceeds Telegram's message limit"
    assert "payload.bullets.pop()" in source, "trimming must drop bullets, not images"


def test_install_page_actually_contains_the_shortcut_javascript():
    """The copy button is useless without it, and a template edit silently dropped it
    once: the button called getElementById on a missing node and died on the first tap."""
    import re

    tools = Path(__file__).resolve().parent.parent / "tools"
    page = (tools / "install.html").read_text()

    box = re.search(r'<textarea[^>]*id="js"[^>]*>(.*?)</textarea>', page, re.S)
    assert box, "the install page has no #js textarea for the copy button to read"

    body = box.group(1)
    assert len(body) > 2000, f"only {len(body)} chars of JavaScript on the page"
    assert "completion(" in body, "the page is not carrying the Shortcut build"
    assert "__SHORTCUT_JS__" not in page, "placeholder was never substituted"


def test_install_page_template_keeps_the_placeholder():
    """Guards the other half: a template without the placeholder builds an empty page."""
    tools = Path(__file__).resolve().parent.parent / "tools"
    template = (tools / "build_bookmarklet.py").read_text()
    page_start = template.index('PAGE = """')
    page_end = template.index('"""', template.index("</html>"))
    assert "__SHORTCUT_JS__" in template[page_start:page_end], (
        "the PAGE template lost its __SHORTCUT_JS__ placeholder"
    )


def test_bookmarklet_does_not_depend_on_desktop_container_ids():
    """amazon.com serves a different DOM to a phone; the capture must survive it."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "bookmarklet.js").read_text()
    assert "document.images" in source, (
        "image collection must sweep every image, not only known container ids"
    )
    assert "main-image-container" in source, "mobile gallery container is not covered"
    assert "[id*='featurebullets']" in source, "mobile bullet container is not covered"


def test_hidden_bullets_are_filtered_in_js_not_by_selector():
    """A selector-level :not(.aok-hidden) is bypassed by the broader fallbacks."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "bookmarklet.js").read_text()
    assert "getComputedStyle" in source
    assert "aok-hidden" in source


def test_bookmarklet_excludes_interface_chrome_from_images():
    """A real mobile capture returned Amazon's own icons: SVGs and 24-60px PNGs served
    from the same CDN path as product photos. Size and format are the discriminators."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "bookmarklet.js").read_text()
    assert "bigEnough" in source, "the broad image sweep has no size gate"
    assert "\\\\.svg" in source or ".svg" in source, "SVG icons are not excluded"


def test_bookmarklet_has_a_title_fallback_for_pages_without_producttitle():
    """#productTitle does not exist on the mobile page; <title> always does."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "bookmarklet.js").read_text()
    assert "document.title" in source
    assert "productTitle()" in source


def test_bookmarklet_reads_the_gallery_from_inline_json():
    """Mobile renders only the visible photo: a real capture got 1 of 10. The rest are
    in inline JSON, so the DOM alone is not enough."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "bookmarklet.js").read_text()
    assert "fromStateScripts" in source
    assert "a-state" in source
    assert "documentElement.innerHTML" in source, "no raw-HTML fallback"


def test_raw_html_sweep_is_gated_on_finding_almost_nothing():
    """Ungated it would pull in recommended products, which carry the same size tokens."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "bookmarklet.js").read_text()
    assert "unique(candidates).length < 3" in source
