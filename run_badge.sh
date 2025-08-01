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
# Set your Meshtastic device MAC address here (the only place you need to change it)
MESHTASTIC_BLE_ADDR="48:CA:43:3C:51:FD"  # Change this to your actual device MAC

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
UI="$PROJECT_DIR/meshtastic-retro-ui.py"

echo "👤 Running as: $(whoami)"
echo "🔧 Target BLE device: $MESHTASTIC_BLE_ADDR"
echo "🐍 Python executable: $VENV/bin/python"
"$VENV/bin/python" --version

# Check if venv exists
if [ ! -f "$VENV/bin/python" ]; then
    echo "❌ Virtual environment not found at $VENV"
    echo "Please run setup.sh first"
    exit 1
fi

# Check if UI script exists
if [ ! -f "$UI" ]; then
    echo "❌ UI script not found at $UI"
    exit 1
fi

echo "🔧 Bringing up BLE adapter..."
sudo rfkill unblock bluetooth || echo "⚠️  rfkill failed (might not be available)"
sudo hciconfig hci0 up || {
    echo "❌ Failed to bring up hci0"
    echo "Available adapters:"
    hciconfig -a
    exit 1
}

# Power on bluetooth
echo -e 'power on\nquit' | timeout 10s bluetoothctl >/dev/null || echo "⚠️  bluetoothctl power on failed"

# Wait for BLE to be ready
sleep 2

echo "🔍 Checking BLE status..."
if ! hciconfig hci0 | grep -q "UP RUNNING"; then
    echo "❌ BLE adapter is not running"
    hciconfig hci0
    exit 1
fi



# Check if target device is in bluetooth cache (non-blocking)
if bluetoothctl info "$MESHTASTIC_BLE_ADDR" >/dev/null 2>&1; then
    echo "✅ Target device $MESHTASTIC_BLE_ADDR found in bluetooth cache"
else
    echo "⚠️  Target device $MESHTASTIC_BLE_ADDR not in cache (this is usually fine)"
fi

echo "🚀 Launching Meshtastic UI..."
source "$VENV/bin/activate"

# Check Python dependencies
if ! python -c "import meshtastic" 2>/dev/null; then
    echo "❌ meshtastic package not found in venv"
    echo "Please run: pip install meshtastic"
    exit 1
fi
if ! python -c "import pubsub" 2>/dev/null; then
    echo "❌ pubsub package not found in venv"
    pip install pypubsub
    exit 1
fi

# remove any python BLEInterface tests
echo "📱 Starting UI with target device: $MESHTASTIC_BLE_ADDR"
export MESHTASTIC_BLE_ADDR="$MESHTASTIC_BLE_ADDR"
exec python "$UI"
