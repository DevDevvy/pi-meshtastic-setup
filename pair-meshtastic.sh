#!/usr/bin/env bash
set -e
DEVICE_MAC="AA:BB:CC:DD:EE:FF"   # ← your Heltec V3 MAC

# Pair & trust the device
bluetoothctl <<EOF
power on
agent on
default-agent
scan on
# wait ~10s for your MAC to appear…
scan off
pair $DEVICE_MAC
trust $DEVICE_MAC
connect $DEVICE_MAC
exit
EOF

# Bind RFCOMM channel 1
sudo rfcomm bind /dev/rfcomm0 $DEVICE_MAC 1

echo "Meshtastic available at /dev/rfcomm0"
