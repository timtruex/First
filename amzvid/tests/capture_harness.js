/* Runs tools/bookmarklet.js against a fixture in jsdom and prints the payload as JSON.
 *
 * The capture script is the part of this project that has broken most often, and it
 * cannot be exercised from Python because it is browser code. This harness gives it the
 * two things it touches - a DOM and a clipboard - and nothing else.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const fixture = process.argv[2];
const script = fs.readFileSync(path.join(__dirname, "..", "tools", "bookmarklet.js"), "utf8");

const dom = new JSDOM(fs.readFileSync(fixture, "utf8"), {
  url: "https://www.amazon.com/dp/B0G37245M5",
  runScripts: "outside-only",
});

let captured = null;
let failure = null;
dom.window.navigator.clipboard = { writeText: (t) => { captured = t; return Promise.resolve(); } };
dom.window.prompt = (_msg, value) => { captured = value; return value; };
// The script falls back to a textarea + execCommand when the async clipboard is absent.
dom.window.document.execCommand = () => {
  const el = dom.window.document.querySelector("textarea");
  if (el) captured = el.value;
  return true;
};

// jsdom does not lay pages out, so every element measures zero. Serve the authored
// width/height attributes instead, which is what a real browser reports for a rendered
// image and what the selection logic is written against.
const proto = dom.window.HTMLImageElement.prototype;
for (const [prop, attr] of [["naturalWidth", "width"], ["naturalHeight", "height"]]) {
  Object.defineProperty(proto, prop, {
    get() { return parseInt(this.getAttribute(attr) || "0", 10); },
  });
}
Object.defineProperty(dom.window.HTMLElement.prototype, "offsetWidth", {
  get() { return parseInt(this.getAttribute("width") || "0", 10); },
});
Object.defineProperty(dom.window.HTMLElement.prototype, "offsetHeight", {
  get() { return parseInt(this.getAttribute("height") || "0", 10); },
});

try {
  dom.window.eval(script);
} catch (err) {
  failure = String(err && err.stack ? err.stack : err);
}

if (failure) {
  console.log(JSON.stringify({ harness_error: failure }));
} else if (captured === null) {
  // The refusal path shows a banner rather than writing to the clipboard. In the iOS
  // Shortcut build the same call becomes completion(), so a refusal is an outcome the
  // tests need to see, not a missing emission.
  const el = dom.window.document.body.querySelector("div[style*='2147483647']");
  console.log(JSON.stringify(
    el ? { refused: el.textContent } : { harness_error: "the script exited without emitting anything" }
  ));
} else {
  console.log(captured);
}
