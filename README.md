<!-- README.md -->

# Retro Meshtastic Badge (Headless Touch Edition)

A 3.5″ touchscreen “badge” UI for Meshtastic via BLE on Raspberry Pi.

## Overview

- **UI script:** `meshtastic-retro-ui.py`
- **Launcher:** `run_badge.sh` (injects your device’s MAC via env var)
- **Persistence:** `~/.retrobadge/meshtastic.db` & `meshtastic.log`
- **Service:** Runs at boot via `systemd` as `meshtastic-badge.service`

## Prerequisites

- Raspberry Pi (any model with BLE + 3.5″ XPT2046 touchscreen hat installed)
- Raspbian or Raspberry Pi OS (Bullseye/Buster) up to date
- Local network or power-on access (headless via SSH or attached keyboard)

## Setup

1. Clone or unpack this repo into your Pi’s home directory:

   ```
   cd /home/pi
   ```

   ```
   git clone https://github.com/DevDevvy/pi-meshtastic-setup.git
   ```

   ```
   cd pi-meshtastic-badge
   ```

2. Run the provisioning script once (requires sudo):
   ```
   sudo ./setup.sh
   ```

## Configuration

1. Set your node’s MAC in run_badge.sh at the top (replace the value of MESHTASTIC_BLE_ADDR="EnterYourMAC")

2. Make sure both run_badge.sh and meshtastic-retro-ui.py are executable:

```
chmod +x run_badge.sh meshtastic-retro-ui.py
```

## Pairing Your Meshtastic Node

```
sudo bluetoothctl
```

⇒ at the prompt:

```
=> power on
=> agent on
=> default-agent
=> scan on (Wait until you see your node)
=> scan off
=> pair <YOUR_NODE_MAC> (input pairing code if needed)
=> trust <YOUR_NODE_MAC>
quit
```

## Running the UI

```
./run_badge.sh
```

## Logs and DB

- Messages log: ~/.retrobadge/meshtastic.log

- SQLite DB: ~/.retrobadge/meshtastic.db

---

Enjoy your retro BLE badge! If you run into issues, verify:

Your node is paired/trusted via bluetoothctl.

run_badge.sh has the correct MAC.

The Python venv has meshtastic, bleak, and pypubsub installed.

Bluetooth adapter is up: rfkill list bluetooth & hciconfig hci0.
