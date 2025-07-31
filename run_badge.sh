#!/usr/bin/env bash
# ----------------------------------------------------------------------
# Wrapper that:
#   • Brings up the BLE adapter
#   • Verifies it can talk to your node
#   • Activates the Python venv
#   • Launches the curses UI (using BLEInterface)
# Called automatically by systemd (see setup.sh)
# ----------------------------------------------------------------------
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────
MESHTASTIC_BLE_ADDR="48:CA:43:3C:51:FD"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
UI="$PROJECT_DIR/meshtastic-retro-ui.py"

echo "👤 Running as: $(whoami)"
echo "🐍 Python executable: $VENV/bin/python"
"$VENV/bin/python" --version

echo "🔧 Bringing up BLE adapter (hci0)…"
sudo rfkill unblock bluetooth
hciconfig hci0 up || true

echo "🔎 Verifying BLE connection to $MESHTASTIC_BLE_ADDR…"
# Use the Meshtastic CLI in the venv to test reachability
if ! "$VENV/bin/meshtastic" --ble "$MESHTASTIC_BLE_ADDR" --info >/dev/null 2>&1; then
  echo "❌  Unable to reach Meshtastic node at $MESHTASTIC_BLE_ADDR – aborting."
  exit 1
fi
echo "✅  Node reachable over BLE."

# ── Launch UI ───────────────────────────────────────────────────────────────
source "$VENV/bin/activate"
exec python "$UI"
