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
OUT_SHORTCUT = HERE / "shortcut.js"

SAFE = "!#$&'()*+,-./:;=?@_~[]{}|^%"


def build_url(js: str) -> str:
    js = re.sub(r"\A/\*.*?\*/\s*", "", js, flags=re.S)  # leading block comment only
    return "javascript:" + quote(js, safe=SAFE)


OUTPUT_BLOCK = re.compile(
    r"  /\* OUTPUT-START:.*?/\* OUTPUT-END \*/", re.S
)
SHORTCUT_TAIL = """  // Shortcuts takes the action's output from completion(), and hangs if it is never
  // called, so failures are reported as a payload too rather than returning silently.
  function emit(payload, message) {
    completion(JSON.stringify(payload || { amzvid: 1, error: message }));
  }"""


def build_shortcut(js: str) -> str:
    """The same extraction, returning through completion() instead of the clipboard.

    Shortcuts' "Run JavaScript on Web Page" evaluates the script in the shared page and
    takes whatever is passed to completion() as the action's output.
    """
    js = re.sub(r"\A/\*.*?\*/\s*", "", js, flags=re.S)
    out, count = OUTPUT_BLOCK.subn(SHORTCUT_TAIL, js)
    if count != 1:
        raise SystemExit("could not find exactly one OUTPUT-START/END block in bookmarklet.js")
    return (
        "// amzvid capture, for Shortcuts' \"Run JavaScript on Web Page\" action.\n"
        "// Generated from tools/bookmarklet.js - edit that file, then rebuild.\n"
        + out
    )


def build_page(url: str, shortcut: str) -> str:
    def esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return (
        PAGE
        .replace("__BOOKMARKLET__", url.replace("&", "&amp;").replace('"', "&quot;"))
        .replace("__SHORTCUT_JS__", esc(shortcut))
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>amzvid Capture</title>
<style>
  :root {
    --bg: #faf9f7; --panel: #ffffff; --ink: #16181d; --muted: #5c6270;
    --line: #e4e2dd; --accent: #c89600; --accent-ink: #16181d; --code: #f3f1ec;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #121317; --panel: #1b1d23; --ink: #f2f3f5; --muted: #a2a8b6;
      --line: #2b2e37; --accent: #ffd736; --accent-ink: #16181d; --code: #15171c;
    }
  }
  :root[data-theme="dark"] {
    --bg: #121317; --panel: #1b1d23; --ink: #f2f3f5; --muted: #a2a8b6;
    --line: #2b2e37; --accent: #ffd736; --accent-ink: #16181d; --code: #15171c;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    padding: 40px 16px 80px;
  }
  main { max-width: 680px; margin: 0 auto; }
  h1 { font-size: 30px; line-height: 1.2; margin: 0 0 8px; letter-spacing: -0.02em; }
  .sub { color: var(--muted); margin: 0 0 32px; }
  h2 { font-size: 20px; margin: 40px 0 6px; letter-spacing: -0.01em; }
  h3 { font-size: 16px; margin: 0 0 14px; letter-spacing: -0.01em; }
  .when { color: var(--muted); font-size: 14px; margin: 0 0 18px; }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 14px; padding: 24px; margin-bottom: 12px;
  }
  .drag { text-align: center; }
  .bm {
    display: inline-block; background: var(--accent); color: var(--accent-ink);
    font-weight: 700; font-size: 17px; text-decoration: none;
    padding: 13px 30px; border-radius: 9px; cursor: grab;
  }
  .hint { color: var(--muted); font-size: 14px; margin: 16px 0 0; }
  ol { padding-left: 22px; margin: 0; }
  li { margin-bottom: 12px; }
  code {
    background: var(--code); border: 1px solid var(--line); border-radius: 5px;
    padding: 2px 6px; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    overflow-wrap: anywhere;  /* the Telegram URL is longer than a phone is wide */
  }
  textarea.js {
    display: block; width: 100%; height: 220px; resize: vertical;
    background: var(--code); color: var(--ink);
    border: 1px solid var(--line); border-radius: 10px; padding: 14px;
    font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    -webkit-appearance: none;
  }
  .fallback { color: var(--muted); font-size: 14px; margin: 10px 0 0; }
  button.copy {
    background: var(--accent); color: var(--accent-ink); border: 0; cursor: pointer;
    font: 600 15px/1 inherit; padding: 12px 20px; border-radius: 8px; margin-bottom: 12px;
  }
  .note {
    border-left: 3px solid var(--accent); background: var(--panel);
    padding: 14px 18px; border-radius: 0 10px 10px 0; margin-top: 40px;
    color: var(--muted); font-size: 15px;
  }
  .note strong { color: var(--ink); }
</style>
</head>
<body>
<main>
  <h1>amzvid Capture</h1>
  <p class="sub">One action on an Amazon listing sends the title, bullets, price and
  photos to your bot. Two ways in: a bookmarklet on the Mac, a Shortcut on the phone.</p>

  <h2>On iPhone</h2>
  <p class="when">Share sheet in Safari. About ten minutes to set up, once.</p>

  <div class="card">
    <h3>Before you start</h3>
    <ol>
      <li><strong>Turn on scripting.</strong> Open the <strong>Settings</strong> app &rarr;
        <strong>Apps</strong> &rarr; <strong>Shortcuts</strong> &rarr; <strong>Advanced</strong>,
        and switch on <strong>Allow Running Scripts</strong>. Shortcuts refuses to run
        JavaScript until you do, and the error it gives does not say so clearly.
        On older iOS this lives at Settings &rarr; Shortcuts &rarr; Advanced.</li>
      <li><strong>Get your two Telegram values.</strong> In Telegram, message
        <strong>@userinfobot</strong> and it replies with your numeric <em>Id</em> — that
        is your <code>chat_id</code>. Your bot token came from <strong>@BotFather</strong>
        when you created the bot; it looks like
        <code>8123456789:AAH...</code>.</li>
    </ol>
  </div>

  <div class="card">
    <h3>Build the shortcut</h3>
    <button class="copy" id="copy">Copy the JavaScript</button>
    <ol>
      <li>Open <strong>Shortcuts</strong> and tap <strong>+</strong> (top right) to start
        a new one.</li>
      <li>Tap <strong>New Shortcut</strong> at the top and rename it
        <strong>amzvid Capture</strong>.</li>
      <li>Tap <strong>Add Action</strong>, search for
        <strong>Run JavaScript on Web Page</strong>, and tap it.</li>
      <li>Tap the grey placeholder script inside that action. Select all of it, delete it,
        and paste the JavaScript from the button above.</li>
      <li>Tap <strong>Add Action</strong> again, search
        <strong>Get Contents of URL</strong>, and tap it.</li>
      <li>In the URL field, type:<br>
        <code>https://api.telegram.org/botYOUR_TOKEN/sendMessage</code><br>
        with your real token in place of <code>YOUR_TOKEN</code>. Keep the word
        <code>bot</code> immediately before it, with no space.</li>
      <li>Tap the small <strong>&gt;</strong> (or <strong>Show More</strong>) on that
        action to expand it.</li>
      <li>Set <strong>Method</strong> to <strong>POST</strong>.</li>
      <li>Set <strong>Request Body</strong> to <strong>Form</strong>. (JSON also works but
        takes more taps and has been buggy in some versions.)</li>
      <li>Tap <strong>Add new field</strong> &rarr; <strong>Text</strong>. Key:
        <code>chat_id</code>. Value: your numeric id from @userinfobot.</li>
      <li>Tap <strong>Add new field</strong> &rarr; <strong>Text</strong> again. Key:
        <code>text</code>. For the value, tap the field, then tap the
        <strong>variable</strong> button just above the keyboard and choose
        <strong>JavaScript Result</strong>. It should appear as a blue pill, not as
        typed-out words.</li>
      <li>Open the shortcut's settings — the <strong>ⓘ</strong> button at the bottom, or
        the toggles icon at the top right depending on your iOS version — and switch on
        <strong>Show in Share Sheet</strong>.</li>
      <li>Still in there, tap <strong>Share Sheet Types</strong> and turn
        <em>everything</em> off except <strong>Safari web pages</strong>. Without this the
        shortcut shows up where it cannot work.</li>
      <li>Tap <strong>Done</strong>.</li>
    </ol>

    <textarea class="js" id="js" readonly spellcheck="false" autocapitalize="off"
      autocorrect="off">__SHORTCUT_JS__</textarea>
    <p class="fallback">If the button does not copy, tap inside the box, then
    <strong>Select All</strong> &rarr; <strong>Copy</strong> from the menu.</p>
  </div>

  <div class="card">
    <h3>Using it</h3>
    <ol>
      <li>Open the Amazon listing <strong>in Safari</strong>. The Amazon app will not work
        — this action only reads Safari pages.</li>
      <li><strong>Scroll the photo gallery once</strong> so the images load. The capture
        reads what the page has actually loaded.</li>
      <li>Tap <strong>Share</strong> &rarr; <strong>amzvid Capture</strong>. You may have
        to scroll down the share sheet, or tap <em>Edit Actions</em> once to pin it.</li>
      <li>The first time, iOS asks whether to allow the shortcut to run on that site.
        Tap <strong>Allow</strong>.</li>
      <li>Check Telegram. The bot replies with the script and a <strong>Render</strong>
        button.</li>
    </ol>
  </div>

  <div class="card">
    <h3>If it does not work</h3>
    <ol>
      <li><strong>Nothing appears in Telegram.</strong> Run the shortcut once from the
        Shortcuts app with an Amazon page open in Safari, and watch the
        <em>Get Contents of URL</em> result. Telegram answers with
        <code>{"ok":false,"description":"..."}</code> and the description names the
        problem — usually a wrong token or chat id.</li>
      <li><strong>The shortcut hangs or times out.</strong> Almost always
        <strong>Allow Running Scripts</strong> being off.</li>
      <li><strong>The bot replies "scroll the photo gallery once".</strong> The capture
        worked but the page had not loaded its images yet. Scroll and share again.</li>
      <li><strong>The shortcut is missing from the share sheet.</strong> Check
        <em>Show in Share Sheet</em> is on and that <em>Safari web pages</em> is among the
        allowed types.</li>
    </ol>
  </div>

  <h2>On the Mac</h2>
  <p class="when">Bookmarks bar in any desktop browser.</p>
  <div class="card drag">
    <a class="bm" href="__BOOKMARKLET__">amzvid Capture</a>
    <p class="hint">Drag this to your bookmarks bar. Clicking it here does nothing.</p>
  </div>
  <div class="card">
    <ol>
      <li>Show the bookmarks bar: <code>⇧⌘B</code> in Chrome,
        <strong>View → Show Favourites Bar</strong> in Safari.</li>
      <li>On a listing, scroll the photo gallery once so the images load, then click
        the button. A green banner confirms what it copied.</li>
      <li>Paste into Telegram, or run <code>amzvid watch</code> and it is picked up
        automatically.</li>
    </ol>
  </div>

  <div class="note">
    <strong>Why this and not a scraper.</strong> Both routes run in your own browser, on
    a page you are already looking at, when you act. There is no automated request to
    Amazon, so there is nothing to rate-limit or block, and it stays clear of the
    Associates agreement's bar on automated access. It is a structured copy-paste.
  </div>
</main>
<script>
  // Clipboard access can be blocked here (an embedded page, or iOS being strict), so
  // always leave the text selected: then the native Copy menu is one tap away even
  // when every programmatic route fails.
  document.getElementById("copy").addEventListener("click", function () {
    var box = document.getElementById("js");
    var button = this;
    var say = function (msg) {
      button.textContent = msg;
      setTimeout(function () { button.textContent = "Copy the JavaScript"; }, 4000);
    };

    box.focus();
    box.setSelectionRange(0, box.value.length);

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(box.value).then(
        function () { say("Copied"); },
        function () { legacy(); }
      );
    } else {
      legacy();
    }

    function legacy() {
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      say(ok ? "Copied" : "Selected - now tap Copy");
    }
  });
</script>
</script>
</body>
</html>
"""


def main() -> int:
    if not SOURCE.exists():
        print(f"missing {SOURCE}", file=sys.stderr)
        return 1
    source = SOURCE.read_text()
    url = build_url(source)
    shortcut = build_shortcut(source)
    OUT_URL.write_text(url + "\n")
    OUT_SHORTCUT.write_text(shortcut)
    OUT_HTML.write_text(build_page(url, shortcut))
    print(f"bookmarklet: {len(url)} chars -> {OUT_URL}")
    print(f"shortcut js: {len(shortcut)} chars -> {OUT_SHORTCUT}")
    print(f"install page -> {OUT_HTML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
