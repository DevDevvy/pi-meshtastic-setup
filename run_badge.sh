#!/usr/bin/env bash
# ----------------------------------------------------------------------
# Wrapper that:
#   • Brings up the BLE adapter
#   • Activates the Python venv
#   • Launches the curses UI (now using BLEInterface)
# Called automatically by systemd (see setup.sh)
# ----------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
UI="$PROJECT_DIR/meshtastic-retro-ui.py"

echo "👤 Running as: $(whoami)"
echo "🐍 Python executable: $VENV/bin/python"
"$VENV/bin/python" --version

echo "🔧 Bringing up BLE adapter (hci0)…"
# Make sure Bluetooth is powered on
sudo rfkill unblock bluetooth
hciconfig hci0 up || true

echo "🔎 MESHTASTIC_BLE_ADDR: ${MESHTASTIC_BLE_ADDR:-not set}"
# Reminder: export MESHTASTIC_BLE_ADDR=11:22:33:44:55:66 (your node's BLE MAC)

# Activate venv & launch UI
# shellcheck disable=SC1090
source "$VENV/bin/activate"

exec python "$UI"
