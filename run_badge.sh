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

echo "📡 Checking for devices..."
timeout 5s bluetoothctl scan on >/dev/null 2>&1 &
SCAN_PID=$!
sleep 3
kill $SCAN_PID 2>/dev/null || true

echo "Available BLE devices:"
bluetoothctl devices | head -10

# Test connection to target device if specified
if [ -n "${MESHTASTIC_BLE_ADDR:-}" ]; then
    echo "🎯 Testing target device $MESHTASTIC_BLE_ADDR..."
    if bluetoothctl info "$MESHTASTIC_BLE_ADDR" >/dev/null 2>&1; then
        echo "✅ Device found in bluetooth cache"
    else
        echo "⚠️  Device not in cache, will try to discover during connection"
    fi
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
    echo "⚠️  pubsub package not found, message receiving may not work"
    echo "Consider running: pip install pypubsub"
fi

# Test BLE permissions and verify target device
echo "🔐 Testing BLE and verifying target device..."
python -c "
from meshtastic.ble_interface import BLEInterface
import traceback
import os

target_addr = os.environ.get('MESHTASTIC_BLE_ADDR', '48:CA:43:3C:51:FD')
print(f'Target device: {target_addr}')

try:
    print('Scanning for BLE devices...')
    devices = BLEInterface.scan()
    print(f'Found {len(devices)} BLE devices:')
    
    target_found = False
    for d in devices:
        print(f'  {d.name} @ {d.address}')
        if d.address.upper() == target_addr.upper():
            print(f'✅ Target device found: {d.name}')
            target_found = True
    
    if not target_found:
        print(f'⚠️  Target device {target_addr} not found in scan')
        print('Will attempt connection anyway...')
        
except Exception as e:
    print(f'BLE scan failed: {e}')
    traceback.print_exc()
    print('Will attempt connection anyway...')
"

echo "📱 Starting UI with target device: $MESHTASTIC_BLE_ADDR"
echo "📝 Logs will be written to: ~/.retrobadge/meshtastic.log"
echo "💾 Messages will be stored in: ~/.retrobadge/meshtastic.db"
echo ""

export MESHTASTIC_BLE_ADDR="$MESHTASTIC_BLE_ADDR"
exec python "$UI"
