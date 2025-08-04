# setup.sh
#!/usr/bin/env bash
# ----------------------------------------------------------------------
# Initial provisioning script for the Meshtastic Retro Badge on a Pi.
# * Run ONCE with sudo from your project root
# * Installs system deps, creates Python venv, installs packages,
#   sets up systemd service to launch run_badge.sh at boot
# ----------------------------------------------------------------------
set -euo pipefail

# Who invoked sudo?
REAL_USER="${SUDO_USER:-}"
if [[ -z "$REAL_USER" ]]; then
  echo "❌  Please run this script with sudo:"
  echo "    sudo ./setup.sh"
  exit 1
fi

PROJECT_DIR="$(pwd)"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_FILE="/etc/systemd/system/meshtastic-badge.service"

echo "🔧 Provisioning for user: $REAL_USER"
echo "📂 Project dir:     $PROJECT_DIR"

echo "1/7 Updating system packages…"
apt update && apt upgrade -y

echo "2/7 Enabling SPI & I2C interfaces…"
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0

echo "3/7 Unblocking & enabling Bluetooth…"
rfkill unblock bluetooth
systemctl enable bluetooth
systemctl start  bluetooth

echo "4/7 Adding $REAL_USER to required groups…"
usermod -aG bluetooth,dialout "$REAL_USER"

echo "5/7 Installing system dependencies…"
apt install -y \
    python3 python3-venv python3-pip git \
    bluez bluez-tools rfkill libbluetooth-dev libffi-dev \
    python3-dev

echo "6/7 Setting up Python virtual environment…"
# ensure project dir owner is correct
chown -R "$REAL_USER":"$REAL_USER" "$PROJECT_DIR"
# create & activate venv
sudo -u "$REAL_USER" python3 -m venv "$VENV_DIR"
# install Python packages
sudo -u "$REAL_USER" bash -c "source '$VENV_DIR/bin/activate' \
  && pip install --upgrade pip \
  && pip install meshtastic[ble] bleak pypubsub"

echo
echo "✅  Setup complete!"
echo "• Edit run_badge.sh to set MESHTASTIC_BLE_ADDR to your node’s MAC"
echo "• See README.md for pairing instructions & usage"
