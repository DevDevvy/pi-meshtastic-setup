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

# Activate venv & launch UI
# shellcheck disable=SC1090
source "$VENV/bin/activate"

exec python "$UI"
