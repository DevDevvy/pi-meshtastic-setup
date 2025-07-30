#!/usr/bin/env bash
set -e
DEVICE_MAC="AA:BB:CC:DD:EE:FF"   # ← your Heltec V3 MAC

sudo bluetoothctl <<EOF
power on
agent on
default-agent
scan on
EOF

# Give it a few seconds to discover the device
sleep 3

sudo bluetoothctl <<EOF
scan off
pair $DEVICE_MAC
trust $DEVICE_MAC
connect $DEVICE_MAC
exit
EOF

# Now bind RFCOMM channel 1
sudo rfcomm bind /dev/rfcomm0 $DEVICE_MAC 1

echo "✅ Bound Meshtastic at /dev/rfcomm0"
