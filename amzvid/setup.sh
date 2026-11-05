#!/usr/bin/env bash
# One-time setup on a Mac. Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
command -v ffmpeg >/dev/null || brew install ffmpeg

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -e ".[dev]"

CONFIG="$HOME/.config/amzvid/config.toml"
if [ ! -f "$CONFIG" ]; then
  mkdir -p "$(dirname "$CONFIG")"
  cp config.example.toml "$CONFIG"
  echo "Wrote $CONFIG - open it and fill in affiliate_tag and voice_id."
fi

mkdir -p assets/fonts assets/music

if [ ! -f assets/fonts/.keep ]; then
  touch assets/fonts/.keep
  cat <<'NOTE'

Fonts: the captions look best in a heavy condensed sans. Download Montserrat
ExtraBold from fonts.google.com and drop Montserrat-ExtraBold.ttf into
assets/fonts/. Without it the pipeline falls back to Arial Black.

Music: drop a few royalty-free loops into assets/music/. One is picked per job,
deterministically, and ducked under the voiceover. Leave it empty for no music.

NOTE
fi

echo "Checking the setup:"
./.venv/bin/amzvid doctor || true

cat <<'NEXT'

Next:
  1. Fill in ~/.config/amzvid/config.toml
  2. export ELEVENLABS_API_KEY=... ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=...
  3. Free end-to-end test (no API calls):
       ./.venv/bin/amzvid run "https://www.amazon.com/dp/B0EXAMPLE" --images ~/Desktop/photos
     with dry_run = true in the config.
  4. Start the bot:
       ./.venv/bin/amzvid bot

NEXT
