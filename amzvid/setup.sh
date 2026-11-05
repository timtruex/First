#!/usr/bin/env bash
# One-time setup on a Mac. Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
command -v ffmpeg >/dev/null || brew install ffmpeg

# macOS ships Python 3.9 as `python3`, and this project needs 3.11+. Look for a
# newer interpreter by name first, fall back to `python3` only if it is new
# enough, and install one from Homebrew as a last resort.
supported() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null && supported "$candidate"; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "No Python 3.11 or newer found (macOS ships 3.9). Installing python@3.12..."
  brew install python@3.12
  PYTHON="$(brew --prefix)/opt/python@3.12/bin/python3.12"
  supported "$PYTHON" || { echo "Homebrew python@3.12 did not install correctly."; exit 1; }
fi
echo "Using $("$PYTHON" --version) from $(command -v "$PYTHON" || echo "$PYTHON")"

# A venv built by an older interpreter cannot be upgraded in place.
if [ -d .venv ] && ! supported ./.venv/bin/python; then
  echo "Existing .venv uses an unsupported Python; rebuilding it."
  rm -rf .venv
fi
if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
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
