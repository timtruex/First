#!/usr/bin/env bash
#
# Install the scanner as a launchd user agent on macOS.
#
# A user agent (~/Library/LaunchAgents) rather than a system daemon: the
# scanner needs no root, and running network-facing code as root for no reason
# is a poor trade. The cost is that it runs only while the user is logged in -
# see the README note on enabling auto-login for a headless Mac mini.

set -euo pipefail

LABEL="com.kalshiarb.scanner"
SCANNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="${SCANNER_DIR}/deploy/${LABEL}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
PYTHON="${PYTHON:-$(command -v python3)}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is macOS-only (found $(uname -s))." >&2
  exit 1
fi

if [[ -z "${PYTHON}" ]]; then
  echo "No python3 on PATH. Install it, or set PYTHON=/path/to/python3." >&2
  exit 1
fi

echo "Python      : ${PYTHON} ($("${PYTHON}" --version))"
echo "Scanner dir : ${SCANNER_DIR}"

# Fail before installing rather than after: a service that installs cleanly
# and then crash-loops on a missing import is harder to debug than a refusal.
echo "Checking dependencies…"
if ! "${PYTHON}" -c "import aiohttp" 2>/dev/null; then
  echo "Missing dependencies. Run:  ${PYTHON} -m pip install -r ${SCANNER_DIR}/requirements.txt" >&2
  exit 1
fi

echo "Checking configuration…"
( cd "${SCANNER_DIR}" && "${PYTHON}" main.py check-config )

mkdir -p "${HOME}/Library/LaunchAgents" "${SCANNER_DIR}/data"

# Unload an existing copy first; launchctl load on an already-loaded label is
# a no-op, which silently leaves the old configuration running.
if launchctl list | grep -q "${LABEL}"; then
  echo "Unloading existing service…"
  launchctl unload "${PLIST_DST}" 2>/dev/null || true
fi

sed -e "s|__PYTHON__|${PYTHON}|g" \
    -e "s|__SCANNER_DIR__|${SCANNER_DIR}|g" \
    "${PLIST_SRC}" > "${PLIST_DST}"

launchctl load -w "${PLIST_DST}"

echo
echo "Installed and started: ${LABEL}"
echo
echo "  status    ${PYTHON} ${SCANNER_DIR}/main.py status"
echo "  logs      tail -f ${SCANNER_DIR}/data/scanner.log"
echo "  stop      launchctl unload -w ${PLIST_DST}"
echo "  start     launchctl load -w ${PLIST_DST}"
echo "  uninstall ${SCANNER_DIR}/deploy/uninstall-macos.sh"
echo
echo "For a 24/7 headless Mac mini, also disable sleep:"
echo "  sudo pmset -a sleep 0 disksleep 0"
