#!/bin/bash
set -e

PROJECT_DIR="/home/pi/meshtastic-badge"

echo "🔧 1. Updating system..."
apt update && apt full-upgrade -y

echo "🔧 2. Enable SPI, I2C (touch), and Bluetooth..."
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0
rfkill unblock bluetooth
systemctl enable bluetooth
systemctl start bluetooth

echo "🔧 3. Add user 'pi' to bluetooth and dialout groups..."
usermod -aG bluetooth,dialout pi

echo "📦 4. Install system dependencies..."
apt install -y \
  python3 python3-venv python3-pip \
  git libffi-dev libbluetooth-dev \
  python3-pygame python3-pil python3-evdev \
  bluez bluez-tools dbus-user-session \
  iw tcpdump libcap2-bin net-tools \
  fonts-dejavu unzip curl

echo "📁 5. Create project directory at $PROJECT_DIR..."
mkdir -p "$PROJECT_DIR"/{logs,cache,assets}
cd "$PROJECT_DIR"

echo "🐍 6. Set up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "⬆️ 7. Upgrade pip & install Python packages..."
pip install --upgrade pip
pip install meshtastic[ble] bleak pybluez scapy

echo "🎨 8. Download retro pixel font..."
curl -L -o assets/pixel_font.ttf \
  https://github.com/adamyg/fonts/raw/master/bitwise/bitwise.ttf

echo "🛠️ 9. Install meshtastic-badge service..."
cat <<EOF | tee /etc/systemd/system/meshtastic-badge.service
[Unit]
Description=Meshtastic Badge Display
After=bluetooth.target network.target

[Service]
ExecStart=$PROJECT_DIR/venv/bin/python $PROJECT_DIR/main.py
WorkingDirectory=$PROJECT_DIR
Restart=always
StandardOutput=journal
StandardError=journal
User=pi

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable meshtastic-badge

echo "✅ Setup complete!"
echo "   • Place your main.py in $PROJECT_DIR"
echo "   • Touchscreen drivers must already be installed"
echo "   • Reboot to start the badge UI: sudo reboot"
