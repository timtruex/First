# amzvid

Local Amazon affiliate review videos. Send a link and photos to a Telegram bot from your
phone; get back a 16:9 long-form review plus three vertical Shorts, each with word-synced
captions, a voiceover, overlays, a thumbnail, and a ready-to-paste YouTube description.

Runs entirely on your Mac. Nothing is uploaded anywhere except the two API calls it makes.

## What it produces per product

| File | Canvas | Typical length |
|---|---|---|
| `<ASIN>_long.mp4` | 1920×1080 | 60–100s |
| `<ASIN>_short1..3.mp4` | 1080×1920 | 12–20s each |
| `thumbnail.jpg` | 1280×720 | — |
| `long.description.txt` | title, tags, description | — |
| `long.metadata.json` | the same, structured | — |

Four uploads per product instead of one, from one script and one voiceover.

## How it differs from a template tool

- **Word-synced captions.** ElevenLabs returns character-level alignment; the pipeline
  turns that into one ASS subtitle event per word, with the spoken word recolored. The
  highlight lands on the syllable rather than drifting a block at a time.
- **Frame-locked timing.** Every scene is rounded up to a whole number of frames, and the
  audio bed is padded onto that same grid. Audio and video stay within half a frame of
  each other over the whole video, verified in the smoke test.
- **Motion that isn't a preset.** Ken Burns moves cycle so no two neighbouring scenes move
  alike, and every `zoompan` expression is a closed form over `on/N` rather than an
  accumulator, so a re-render reproduces the same motion exactly.
- **Whole-product framing.** A square photo on a 16:9 canvas gets a blurred backdrop
  instead of a crop that cuts the top and bottom off the product.
- **A script with a drawback in it.** The prompt requires exactly one honest `con` scene
  and forbids inventing specs, prices, or comparisons that aren't in the supplied data.

## Setup

```bash
./setup.sh
```

Then fill in `~/.config/amzvid/config.toml` (at minimum `affiliate_tag` and
`voice.voice_id`) and export your keys:

```bash
export ELEVENLABS_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."
```

Check everything resolves:

```bash
./.venv/bin/amzvid doctor
```

Drop a heavy sans into `assets/fonts/` (Montserrat ExtraBold works well) and a few
royalty-free loops into `assets/music/`. Both are optional; without music the voiceover
runs dry, and without a font it falls back to Arial Black.

## Free end-to-end test

Set `dry_run = true` in the config. The pipeline then skips both paid APIs, writes a
placeholder script, and voices it as silence with estimated word timings — but renders
everything else for real. Use it to check framing, captions and overlays before spending
anything:

```bash
./.venv/bin/amzvid run "https://www.amazon.com/dp/B0EXAMPLE" --images ~/Desktop/photos
```

## Daily use

```bash
./.venv/bin/amzvid bot
```

Then, from Telegram:

1. Send the affiliate link.
2. Send the product photos (two or more; the first one becomes the hero).
3. Send any research notes as plain text. **These carry more weight than the listing copy**
   — the prompt is told to prefer your angles over Amazon's bullets.
4. `/go`

The script comes back for approval with **Render / Rewrite / Cancel**. Nothing is voiced
or rendered until you tap Render. Finished videos come back in the chat.

Useful commands: `/title`, `/price`, `/status`, `/jobs`, `/cancel`.

### Without Telegram

```bash
amzvid new <url> --images ./photos --title "..." --price '$29.99' --notes "..."
amzvid script <job_id>     # writes and prints the script
amzvid render <job_id>     # voices and renders
amzvid jobs
```

## Running it as a service

```bash
cat > ~/Library/LaunchAgents/com.amzvid.bot.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.amzvid.bot</string>
  <key>ProgramArguments</key>
  <array><string>/Users/YOU/amzvid/.venv/bin/amzvid</string><string>bot</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>ELEVENLABS_API_KEY</key><string>...</string>
    <key>ANTHROPIC_API_KEY</key><string>...</string>
    <key>TELEGRAM_BOT_TOKEN</key><string>...</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/amzvid.log</string>
</dict></plist>
PLIST

launchctl load ~/Library/LaunchAgents/com.amzvid.bot.plist
```

Put the keys in the plist only if you are comfortable with them sitting in a file in your
home directory; otherwise keep using a shell that sources them.

## Product data

`source = "manual"` (the default) uses the photos and copy you supply. It has no
dependency on Amazon's anti-bot posture, and you pick the good photos instead of taking
whatever happens to be image #1.

`source = "paapi"` uses the Product Advertising API 5.0 — the sanctioned route for listing
data and licensed image URLs, requiring an approved Associates account with 3 qualifying
sales in the trailing 180 days. Fill in `[paapi]` and flip `source`; nothing else changes.

There is deliberately no page-scraping path. Amazon blocks scrapers aggressively and the
Associates agreement governs image use; the account is the asset worth protecting.

## Cost per product

| | Approx. |
|---|---|
| Claude (script, ~2k in / 2k out) | under $0.05 |
| ElevenLabs (~200 words) | ~1,200 characters of quota |
| Render | free, ~3–6 min on Apple Silicon |

## Tuning

Everything below lives in `config.toml`.

| Setting | Effect |
|---|---|
| `script.target_words` | Video length. 190 words ≈ 75s. |
| `script.effort` | `high` is the default; `medium` is cheaper and usually fine. |
| `script.channel_voice` | The single biggest lever on how the script reads. |
| `render.caption_words_per_chunk` | 3 is punchier, 5 is calmer. |
| `render.transition_s` | Cross-fade length. |
| `render.jobs` | Parallel scene renders; set to your core count. |
| `voice.speed`, `voice.stability` | ElevenLabs delivery. |
| `shorts_per_video` | How many vertical cuts to produce. |

## Layout

```
amzvid/
  config.py        settings, canvases, encoder detection
  models.py        domain types; ScriptDraft is the LLM contract
  db.py            SQLite job store
  plan.py          draft + audio -> render plan (image assignment, moves, overlays)
  pipeline.py      stage orchestration
  offline.py       dry-run stand-ins for both paid APIs
  sources/         manual + PA-API 5.0 (SigV4 signed, no third-party SDK)
  script/          prompts and the Claude call
  voice/           ElevenLabs with-timestamps, alignment -> word timings
  render/          plates, Ken Burns, ASS captions, overlay cards, ffmpeg passes
  publish/         description, disclosure, chapters, thumbnail
  bot/             Telegram
```

Rendering is two passes. Pass A renders one motion clip per scene per canvas, in
parallel. Pass B cross-fades those clips, overlays the cards, burns the captions and
muxes the audio bed. Separate passes mean a failure names the scene that failed, scenes
parallelize across cores, and a Short can reuse a scene without re-deriving its motion.

## Tests

```bash
./.venv/bin/python -m pytest           # 45 unit tests, under a second
./.venv/bin/python -m pytest -m slow   # end-to-end render, needs ffmpeg, ~2 min
```

The unit tests cover alignment-to-word conversion, caption chunking and ASS output, frame
alignment, `zoompan` expression shape, fit selection, ASIN extraction from every Amazon
URL shape, image assignment, overlay placement, and chapter rules.

The `slow` test renders a real video in dry-run mode and asserts on the output files:
canvas geometry per cut, exact frame rate, **audio/video drift under one frame**, an audio
stream on every cut, thumbnail dimensions, and that the affiliate disclosure leads the
description with the tag intact. It is deselected by default because it needs ffmpeg and
takes about two minutes.

Both run in CI on every push that touches `amzvid/` (`.github/workflows/amzvid.yml`),
across Python 3.11-3.13. The smoke job uploads the rendered MP4s as a build artifact, so
you can watch what CI actually produced rather than trusting a green check.

## Before you publish

- The affiliate disclosure is placed first in every description, as the Associates
  agreement and the FTC require. Don't move it below the fold.
- Prices are never spoken, only shown, with an on-screen "price at recording" label and a
  note in the description.
- YouTube's inauthentic-content policy is the real risk for templated review videos at
  volume. The honest-drawback scene, your own research notes, and varying the hook are
  what keep these on the right side of it.
