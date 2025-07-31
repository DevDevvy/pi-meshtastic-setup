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

echo "👤 Running as: $(whoami)"
echo "🔎 /dev/rfcomm0 permissions:"
ls -l /dev/rfcomm0 || echo "rfcomm0 not found"
echo "🐍 Python in venv: $VENV/bin/python"
"$VENV/bin/python" --version

# (Re)‑bind RFCOMM: always release and rebind to avoid stale connections
if rfcomm show /dev/rfcomm0 &>/dev/null; then
  echo "🔌  Releasing old /dev/rfcomm0 binding …"
  rfcomm release /dev/rfcomm0 || true
fi

# Try to bind and verify rfcomm0 is established before starting UI
MAX_ATTEMPTS=5
for attempt in $(seq 1 $MAX_ATTEMPTS); do
  echo "🔗  Binding /dev/rfcomm0 to $MAC … (attempt $attempt/$MAX_ATTEMPTS)"
  rfcomm bind /dev/rfcomm0 "$MAC" 1 || {
    echo "❌  rfcomm bind failed"; exit 1; }
  sleep 2
  if rfcomm show /dev/rfcomm0 &>/dev/null; then
    echo "✅  /dev/rfcomm0 is established."
    break
  else
    echo "⏳  Waiting for /dev/rfcomm0 to be established..."
    rfcomm release /dev/rfcomm0 || true
    sleep 2
  fi
  if [[ $attempt -eq $MAX_ATTEMPTS ]]; then
    echo "❌  Could not establish /dev/rfcomm0 after $MAX_ATTEMPTS attempts."
    exit 1
  fi
done

# Activate venv & launch UI
# shellcheck disable=SC1090
source "$VENV/bin/activate"

exec python "$UI"
