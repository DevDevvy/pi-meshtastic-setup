#!/usr/bin/env bash
# ----------------------------------------------------------------------
# One‑time helper to pair a Meshtastic node and bind /dev/rfcomm0
# Usage: sudo ./pair-meshtastic.sh AA:BB:CC:DD:EE:FF
# ----------------------------------------------------------------------
set -euo pipefail

MAC="${1:-}"
if [[ -z "$MAC" ]]; then
  echo "Usage: sudo $0 <NODE-BT-MAC>"
  exit 1
fi

echo "🪄  Pairing $MAC … (follow prompts)"
bluetoothctl <<EOF
power on
agent on
default-agent
trust $MAC
pair  $MAC
quit
EOF

echo "🔗  Binding /dev/rfcomm0 to channel 1"
rfcomm bind /dev/rfcomm0 "$MAC" 1 || {
  echo "❌  rfcomm bind failed"; exit 1; }

echo "✅  Node paired and bound.  /dev/rfcomm0 is ready."
