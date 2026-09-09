#!/usr/bin/env bash
set -euo pipefail

LABEL="com.kalshiarb.scanner"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [[ -f "${PLIST_DST}" ]]; then
  launchctl unload -w "${PLIST_DST}" 2>/dev/null || true
  rm -f "${PLIST_DST}"
  echo "Removed ${LABEL}."
else
  echo "${LABEL} is not installed."
fi
echo "Data and logs under data/ were left in place."
