# Retro Meshtastic Badge for Raspberry Pi 4/5

A self‑contained Raspberry Pi “badge” that pairs with a Heltec V3 (or any
Meshtastic‑compatible) radio, shows live mesh traffic in glorious green
retro text, and lets you shoot messages back into the mesh.

<kbd>pygame</kbd> provides a pixel‑art full‑screen display for your
3.5″ XPT2046 touchscreen, while an optional curses UI lets you use the
console over SSH or HDMI.

---

## Features

| Feature             | Details                                                                                        |
| ------------------- | ---------------------------------------------------------------------------------------------- |
| **Auto‑pair BLE**   | Scans for any device whose BLE name starts with `Meshtastic` and connects automatically.       |
| **Real‑time feed**  | Incoming messages scroll on screen with a PETSCII‑style font.                                  |
| **Send messages**   | Type `s` in the terminal UI or tap the touchscreen (future update) to send broadcasts.         |
| **Local cache**     | Messages are persisted to `cache/messages.jsonl` and an optional SQLite DB.                    |
| **Extra nerdiness** | Background threads count nearby BLE MACs & Wi‑Fi SSIDs for DEF CON badge‑wars bragging rights. |

---

## Hardware you’ll need

- Raspberry Pi 4 B or 5 with Wi‑Fi & Bluetooth enabled
- 3.5″ XPT2046 SPI touchscreen (320 × 240)
- Heltec V3 (or similar) running Meshtastic ≥ **2.3**
- Micro‑SD card flashed with **Raspberry Pi OS Bookworm** (32‑ or 64‑bit)
- 5 V power bank if you’re roaming the conference floor

---

## Quick‑start (15 minutes)

```bash
# 1. SSH into the Pi with a fresh Bookworm image
git clone https://github.com/yourname/meshtastic-badge.git
cd meshtastic-badge

# 2. Run the one‑shot installer (reboots at the end)
sudo ./setup.sh
```

Edit pair-meshtastic.sh first!
Replace AA:BB:CC:DD:EE:FF with your Heltec’s BLE MAC (see it in the
Meshtastic phone app).

# 3. Pair & bind RFCOMM

./pair-meshtastic.sh # creates /dev/rfcomm0

# 4. Drop your Heltec on a lanyard, reboot the Pi

sudo reboot

On boot you’ll see the green terminal feed on HDMI or the pixel display
on the TFT. Press ⌃ C (terminal) or tap the top 20 % of the
touchscreen to scroll up; tap lower area to scroll down.

## Customisation

- Font / colours – edit FONT_PATH, BG_COLOR, and FG_COLOR
  constants in main.py.

- Screen size – change SCREEN_WIDTH / SCREEN_HEIGHT if you’re on
  a different display.

- Channel settings – update your Heltec with the same channel key as the rest of the mesh via the Meshtastic phone app or CLI.
