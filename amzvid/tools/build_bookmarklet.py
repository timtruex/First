#!/usr/bin/env python3
"""Turn tools/bookmarklet.js into a javascript: URL and an install page.

Deliberately does no minification beyond dropping the leading licence comment: a
regex-based JS minifier mangles regex literals, and a bookmarklet has no size problem
worth that risk. The source is URL-encoded whole.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "bookmarklet.js"
OUT_HTML = HERE / "install.html"
OUT_URL = HERE / "bookmarklet.url.txt"

SAFE = "!#$&'()*+,-./:;=?@_~[]{}|^%"


def build_url(js: str) -> str:
    js = re.sub(r"\A/\*.*?\*/\s*", "", js, flags=re.S)  # leading block comment only
    return "javascript:" + quote(js, safe=SAFE)


def build_page(url: str) -> str:
    return PAGE.replace("__BOOKMARKLET__", url.replace("&", "&amp;").replace('"', "&quot;"))


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>amzvid Capture</title>
<style>
  :root {
    --bg: #faf9f7; --panel: #ffffff; --ink: #16181d; --muted: #5c6270;
    --line: #e4e2dd; --accent: #c89600; --accent-ink: #16181d;
  }
  :root:not([data-theme="light"]) { }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #121317; --panel: #1b1d23; --ink: #f2f3f5; --muted: #a2a8b6;
      --line: #2b2e37; --accent: #ffd736; --accent-ink: #16181d;
    }
  }
  :root[data-theme="dark"] {
    --bg: #121317; --panel: #1b1d23; --ink: #f2f3f5; --muted: #a2a8b6;
    --line: #2b2e37; --accent: #ffd736; --accent-ink: #16181d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    padding: 48px 16px 80px;
  }
  main { max-width: 680px; margin: 0 auto; }
  h1 { font-size: 30px; line-height: 1.2; margin: 0 0 8px; letter-spacing: -0.02em; }
  .sub { color: var(--muted); margin: 0 0 36px; }
  .drag {
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
    padding: 28px; text-align: center; margin-bottom: 36px;
  }
  .bm {
    display: inline-block; background: var(--accent); color: var(--accent-ink);
    font-weight: 700; font-size: 17px; text-decoration: none;
    padding: 13px 30px; border-radius: 9px; cursor: grab;
  }
  .bm:active { cursor: grabbing; }
  .hint { color: var(--muted); font-size: 14px; margin: 16px 0 0; }
  ol { padding-left: 22px; margin: 0; }
  li { margin-bottom: 14px; }
  li strong { font-weight: 650; }
  code {
    background: var(--panel); border: 1px solid var(--line); border-radius: 5px;
    padding: 2px 6px; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .note {
    border-left: 3px solid var(--accent); background: var(--panel);
    padding: 14px 18px; border-radius: 0 10px 10px 0; margin-top: 36px;
    color: var(--muted); font-size: 15px;
  }
  .note strong { color: var(--ink); }
  h2 { font-size: 18px; margin: 36px 0 14px; }
</style>
</head>
<body>
<main>
  <h1>amzvid Capture</h1>
  <p class="sub">One click on an Amazon listing copies the title, bullets, price and
  photo URLs. Paste it into your bot and you are done typing.</p>

  <div class="drag">
    <a class="bm" href="__BOOKMARKLET__">amzvid Capture</a>
    <p class="hint">Drag this button to your bookmarks bar. Clicking it here does nothing.</p>
  </div>

  <h2>Setup</h2>
  <ol>
    <li>Show your bookmarks bar if it is hidden: <code>⇧⌘B</code> in Chrome,
      <strong>View → Show Favourites Bar</strong> in Safari.</li>
    <li>Drag the yellow button onto the bar.</li>
  </ol>

  <h2>Using it</h2>
  <ol>
    <li>Open the Amazon listing and <strong>scroll the photo gallery once</strong> so the
      images load. The capture reads what the page has loaded.</li>
    <li>Click <strong>amzvid Capture</strong> in your bookmarks bar. A green banner
      confirms what it copied.</li>
    <li>Paste into your Telegram bot, or run <code>amzvid paste --go</code> in a terminal.</li>
  </ol>

  <div class="note">
    <strong>Why a bookmarklet and not a scraper.</strong> This runs in your browser, on a
    page you are already looking at, when you click it. There is no automated request to
    Amazon, so there is nothing to rate-limit or block, and it stays clear of the
    Associates agreement's bar on automated access. It is a structured copy-paste.
  </div>
</main>
</body>
</html>
"""


def main() -> int:
    if not SOURCE.exists():
        print(f"missing {SOURCE}", file=sys.stderr)
        return 1
    url = build_url(SOURCE.read_text())
    OUT_URL.write_text(url + "\n")
    OUT_HTML.write_text(build_page(url))
    print(f"bookmarklet: {len(url)} chars -> {OUT_URL}")
    print(f"install page -> {OUT_HTML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
