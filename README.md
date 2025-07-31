# 📟 Pi Meshtastic Retro Badge

A terminal-based retro Meshtastic interface for 3.5″ Raspberry Pi touchscreens.

- ✅ Bluetooth connection to `/dev/rfcomm0`
- ✅ Message send + receive (via Meshtastic Python API)
- ✅ Scrollable UI (touch, wheel, ↑/↓, PgUp/PgDn)
- ✅ Message log saved to SQLite and JSON
- ✅ Systemd service autostarts at boot

---

## 🧰 What’s Included

| File                     | Description                                            |
| ------------------------ | ------------------------------------------------------ |
| `setup.sh`               | One-time system setup: packages, venv, systemd service |
| `pair-meshtastic.sh`     | One-time Bluetooth pairing and `/dev/rfcomm0` binding  |
| `run_badge.sh`           | Launch script for UI + rebinding if needed             |
| `meshtastic-retro-ui.py` | The curses-based badge UI (message viewer + sender)    |

---

## 🚀 Quick Start

### 1. Clone the Project

```bash
git clone https://github.com/devdevvy/pi-meshtastic-setup.git
cd pi-meshtastic-setup
chmod +x *.sh *.py
sudo ./setup.sh                   # installs everything & enables service
sudo ./pair-meshtastic.sh AA:BB:CC:DD:EE:FF   # once, with your node MAC
sudo reboot
```
