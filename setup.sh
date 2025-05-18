#!/bin/bash

set -e

echo "🔧 Updating system..."
apt update && apt full-upgrade -y

echo "🔧 Enabling SPI, I2C, and Bluetooth..."
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0
rfkill unblock bluetooth
systemctl enable bluetooth
systemctl start bluetooth

echo "🔧 Installing core dependencies..."
apt install -y \
  python3 python3-pip python3-venv \
  git libffi-dev libbluetooth-dev \
  python3-pygame python3-pil \
  bluez bluez-tools \
  iw tcpdump net-tools \
  fonts-dejavu unzip

echo "🔧 Installing Meshtastic with BLE support..."
pip3 install meshtastic[ble] bleak

echo "🔧 Installing scanning tools and BLE libraries..."
pip3 install pybluez scapy

echo "📦 Creating project directories..."
mkdir -p ~/meshtastic-badge/{logs,cache,assets}
cd ~/meshtastic-badge

echo "📦 Downloading retro pixel font..."
curl -L -o assets/pixel_font.ttf https://github.com/adamyg/fonts/raw/master/bitwise/bitwise.ttf

echo "🛠️ Setting up systemd autostart..."
cat <<EOF | sudo tee /etc/systemd/system/meshtastic-badge.service
[Unit]
Description=Meshtastic Badge Display
After=bluetooth.target network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/meshtastic-badge/main.py
WorkingDirectory=/home/pi/meshtastic-badge
StandardOutput=inherit
StandardError=inherit
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable meshtastic-badge

echo "✅ Setup complete. You can now place your main.py code in ~/meshtastic-badge and reboot."
