/* amzvid capture bookmarklet.
 *
 * Runs in your own browser, on a listing page you are already looking at, when you
 * click it. That matters: there is no automated request to Amazon, so nothing to
 * rate-limit, CAPTCHA or block, and it stays on the right side of the Associates
 * agreement's bar on automated access. It is a structured copy-paste.
 *
 * Collects title, brand, price, rating, bullets and the hi-res image URLs, then puts
 * one JSON blob on the clipboard for `amzvid paste` or the Telegram bot.
 */
(function () {
  "use strict";

  var MAX_IMAGES = 10;
  var MAX_BULLET_CHARS = 220;
  /* Telegram's sendMessage caps `text` at 4096 characters and rejects the whole
     message past that. Amazon's electronics bullets run to 500 characters each, so a
     12-bullet listing can produce a 7KB payload that never arrives. Stay well under. */
  var MAX_PAYLOAD_CHARS = 3600;

  function text(selector) {
    var el = document.querySelector(selector);
    return el ? el.textContent.trim().replace(/\s+/g, " ") : "";
  }

  function asin() {
    var fromPath = location.pathname.match(/\/(?:dp|gp\/product|gp\/aw\/d)\/([A-Z0-9]{10})/i);
    if (fromPath) return fromPath[1].toUpperCase();
    var input = document.querySelector("input#ASIN, input[name='ASIN'], input#ASIN_DISPLAY");
    return input && input.value ? input.value.toUpperCase() : "";
  }

  function bullets() {
    var nodes = document.querySelectorAll(
      "#feature-bullets li:not(.aok-hidden) span.a-list-item, " +
      "#featurebullets_feature_div li span.a-list-item, " +
      "#productFactsDesktopExpander li span.a-list-item, " +
      "#productOverview_feature_div li span.a-list-item, " +
      "[id*='feature-bullets'] li span.a-list-item, " +
      "[id*='featurebullets'] li span.a-list-item"
    );
    if (!nodes.length) {
      /* Last resort for layouts none of the above name: any bulleted list sitting in a
         container whose id mentions features or bullets. */
      nodes = document.querySelectorAll(
        "[id*='feature'] ul.a-unordered-list li span, [id*='bullet'] ul li span"
      );
    }
    var seen = {};
    return [].slice.call(nodes)
      /* Hidden bullets carry legal boilerplate. Filter in JS rather than with
         :not(.aok-hidden) on each selector - the broader fallbacks above would bypass
         a selector-level filter, and other layouts hide them other ways. */
      .filter(function (n) {
        var li = n.closest ? n.closest("li") : null;
        var node = li || n;
        if (node.className && /\b(aok-hidden|a-hidden)\b/.test(node.className)) return false;
        if (node.hidden) return false;
        var style = window.getComputedStyle ? window.getComputedStyle(node) : null;
        return !style || (style.display !== "none" && style.visibility !== "hidden");
      })
      .map(function (n) { return n.textContent.trim().replace(/\s+/g, " "); })
      .filter(function (t) {
        if (!t || t.length < 4 || /^see more/i.test(t)) return false;
        if (seen[t]) return false;
        seen[t] = 1;
        return true;
      })
      .map(function (t) {
        return t.length > MAX_BULLET_CHARS ? t.slice(0, MAX_BULLET_CHARS - 1).replace(/\s+\S*$/, "") + "\u2026" : t;
      })
      .slice(0, 12);
  }

  /* Amazon serves one image under many size tokens, so the same photo arrives as
     several distinct URLs. Dedupe on the image id in the path, not the URL, or a
     gallery of one photo looks like a gallery of six. All three sources are read,
     best first, rather than stopping at the first that yields anything: the image
     block carries true hi-res, while the thumbnail strip is often the only place the
     rest of the gallery appears. */
  function imageId(url) {
    var m = url.match(/\/images\/I\/([^./]+)/);
    return m ? m[1] : url;
  }

  function upgrade(url) {
    return url.replace(/\._[A-Z0-9_,]+_\./, "._AC_SL1500_.");
  }

  function usable(url) {
    return url &&
      /(media-amazon|ssl-images-amazon)\.com\/images\/I\//.test(url) &&
      !/(sprite|grey-pixel|transparent-pixel|play-button|360_icon|video)/i.test(url);
  }

  function images() {
    var candidates = [];

    try {
      var block = document.documentElement.innerHTML.match(
        /['"]colorImages['"]\s*:\s*\{\s*['"]initial['"]\s*:\s*(\[[\s\S]*?\])\s*[,}]/
      );
      if (block) {
        JSON.parse(block[1]).forEach(function (entry) {
          var url = entry.hiRes || entry.large || entry.thumb;
          if (url) candidates.push(entry.hiRes ? url : upgrade(url));
        });
      }
    } catch (e) { /* fall through */ }

    [].slice.call(document.querySelectorAll("#landingImage, #imgTagWrapperId img")).forEach(function (img) {
      var hires = img.getAttribute("data-old-hires");
      if (hires) candidates.push(hires);
      var dynamic = img.getAttribute("data-a-dynamic-image");
      if (dynamic) {
        try {
          var map = JSON.parse(dynamic);
          Object.keys(map).sort(function (a, b) {
            return (map[b][0] || 0) - (map[a][0] || 0);
          }).forEach(function (u) { candidates.push(u); });
        } catch (e) { /* ignore */ }
      }
    });

    [].slice.call(document.querySelectorAll(
      "#altImages img, #imageBlockThumbs img, #main-image-container img, " +
      "#imageBlock img, #ivImages img, .a-dynamic-image"
    )).forEach(function (img) {
      if (img.src) candidates.push(upgrade(img.src));
    });

    /* Nothing above is guaranteed on the mobile site, whose container ids differ from
       the desktop one. Every product photo is served from the same CDN path, though, so
       sweep every image on the page as a final pass. usable() does the filtering. */
    [].slice.call(document.images).forEach(function (img) {
      var src = img.currentSrc || img.src;
      if (src) candidates.push(upgrade(src));
      var dyn = img.getAttribute && img.getAttribute("data-a-dynamic-image");
      if (dyn) {
        try {
          Object.keys(JSON.parse(dyn)).forEach(function (u) { candidates.push(u); });
        } catch (e) { /* ignore */ }
      }
    });

    var byId = {};
    var order = [];
    candidates.forEach(function (url) {
      if (!usable(url)) return;
      var id = imageId(url);
      if (byId[id]) return;        // first win: the hi-res sources are read first
      byId[id] = url;
      order.push(id);
    });

    return order.slice(0, MAX_IMAGES).map(function (id) { return byId[id]; });
  }

  function banner(message, good) {
    var el = document.createElement("div");
    el.textContent = message;
    el.setAttribute("style",
      "position:fixed;z-index:2147483647;top:16px;left:50%;transform:translateX(-50%);" +
      "padding:14px 22px;border-radius:10px;font:600 15px/1.4 -apple-system,system-ui,sans-serif;" +
      "box-shadow:0 8px 28px rgba(0,0,0,.3);max-width:80vw;text-align:center;" +
      (good ? "background:#132;color:#7ef0a8;border:1px solid #2a5" : "background:#311;color:#ffb4b4;border:1px solid #a33"));
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, good ? 4000 : 7000);
  }

  function copy(payloadText, summary) {
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = payloadText;
      ta.setAttribute("style", "position:fixed;top:-1000px;left:-1000px;");
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      ta.remove();
      if (ok) banner(summary, true);
      else window.prompt("Copy this, then paste it into amzvid:", payloadText);
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(payloadText).then(function () {
        banner(summary, true);
      }, fallback);
    } else {
      fallback();
    }
  }

  /* OUTPUT-START: the iOS Shortcut build replaces this function's body, because
     Shortcuts returns a value through completion() rather than copying. Every exit
     path below goes through emit(), so neither build can finish silently. */
  function emit(payload, message) {
    if (payload) {
      copy(JSON.stringify(payload), message);
    } else {
      banner("amzvid: " + message, false);
    }
  }
  /* OUTPUT-END */

  var id = asin();
  if (!id) {
    emit(null, "this does not look like a product page (no ASIN found).");
    return;
  }

  var pics = images();
  var feats = bullets();
  var payload = {
    amzvid: 1,
    asin: id,
    url: "https://www.amazon.com/dp/" + id,
    title: text("#productTitle"),
    brand: text("#bylineInfo").replace(/^(visit the|brand:)\s*/i, "").replace(/\s+store$/i, ""),
    price: text(".a-price .a-offscreen") || text("#priceblock_ourprice") ||
           text("#corePrice_feature_div .a-offscreen") || text("#price_inside_buybox"),
    rating: (text("#acrPopover .a-icon-alt") || text("[data-hook='rating-out-of-text']")).replace(/ out of.*/i, ""),
    review_count: text("#acrCustomerReviewText"),
    bullets: feats,
    images: pics,
    captured_at: new Date().toISOString()
  };

  if (!pics.length) {
    /* Report what the page actually held. Guessing at another site's markup from a
       bare "found nothing" costs a round trip each time; a sample of real src values
       says immediately whether the images are absent, lazy, or on a path we reject. */
    var all = [].slice.call(document.images);
    var sample = all
      .map(function (i) { return i.currentSrc || i.src || i.getAttribute("data-src") || ""; })
      .filter(function (u) { return u && u.indexOf("data:") !== 0; })
      .slice(0, 4)
      .map(function (u) { return u.length > 110 ? u.slice(0, 110) + "..." : u; });
    emit(null, "found no usable images. Scroll the photo gallery once, then try again." +
      " [imgs on page: " + all.length +
      ", bullets: " + feats.length +
      ", samples: " + (sample.length ? sample.join(" | ") : "none") + "]");
    return;
  }

  /* Photos matter more to the video than the tail of a bullet list, so trim bullets
     until the payload fits rather than dropping images. */
  while (JSON.stringify(payload).length > MAX_PAYLOAD_CHARS && payload.bullets.length > 1) {
    payload.bullets.pop();
  }
  feats = payload.bullets;

  emit(payload, "amzvid: copied " + pics.length + " image" +
       (pics.length === 1 ? "" : "s") + " and " + feats.length + " bullet" +
       (feats.length === 1 ? "" : "s") + ". Paste it into Telegram.");
})();
