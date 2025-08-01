#!/usr/bin/env bash
# ----------------------------------------------------------------------
# Initial provisioning script for the Meshtastic Retro Badge on a Pi.
# * Run ONCE with sudo
# * Creates Python venv, installs deps, sets up systemd unit
# ----------------------------------------------------------------------
set -euo pipefail

# ── Who is the “real” user (the one who invoked sudo)? ────────────────
REAL_USER="${SUDO_USER:-}"
if [[ -z "$REAL_USER" ]]; then
  echo "❌  Run this script with sudo, e.g.  sudo ./setup.sh"
  exit 1
fi

PROJECT_DIR="/home/$REAL_USER/pi-meshtastic-setup"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_FILE="/etc/systemd/system/meshtastic-badge.service"

echo "🔧 1/7  Updating system…"
apt update && apt full-upgrade -y

echo "🔧 2/7  Enabling SPI, I2C & Bluetooth…"
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0
rfkill unblock bluetooth
systemctl enable bluetooth
systemctl start  bluetooth

echo "🔧 3/7  Adding $REAL_USER to bluetooth & dialout groups…"
usermod -aG bluetooth,dialout "$REAL_USER"

echo "📦 4/7  Installing system dependencies…"
apt install -y \
    python3 python3-venv python3-pip \
    git libffi-dev libbluetooth-dev \
    bluez bluez-tools rfkill \
    unzip curl

echo "📁 5/7  Creating project directory $PROJECT_DIR…"
mkdir -p "$PROJECT_DIR"/{logs,assets}
chown -R "$REAL_USER":"$REAL_USER" "$PROJECT_DIR"
cd "$PROJECT_DIR"

echo "🐍 6/7  Creating Python venv & installing packages…"
python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install meshtastic[ble] 

chmod 644 "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable meshtastic-badge

echo "✅  Setup complete."
echo "• OPTIONAL: pair your radio now → sudo ./pair-meshtastic.sh AA:BB:CC:DD:EE:FF"
echo "• Reboot to launch badge automatically:  sudo reboot"
