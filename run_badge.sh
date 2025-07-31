#!/usr/bin/env bash
# ----------------------------------------------------------------------
# Wrapper that:
#   1. Ensures /dev/rfcomm0 exists (re‑binds if needed)
#   2. Launches the curses UI inside the project’s Python venv
# Called automatically by systemd (see setup.sh)
# ----------------------------------------------------------------------
set -euo pipefail
MAC="48:CA:43:3C:51:FD"           # ← edit to your node’s MAC
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
UI="$PROJECT_DIR/meshtastic-retro-ui.py"

# (Re)‑bind RFCOMM if necessary
if [[ ! -e /dev/rfcomm0 ]]; then
  echo "🔗  Rebinding /dev/rfcomm0 …"
  rfcomm bind /dev/rfcomm0 "$MAC" 1 || {
    echo "❌  rfcomm bind failed"; exit 1; }
fi

# Activate venv & launch UI
# shellcheck disable=SC1090
source "$VENV/bin/activate"
exec python "$UI"
