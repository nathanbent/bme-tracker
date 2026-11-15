#!/bin/bash
# Run on the Pi, from inside this folder:  bash install.sh
# Safe to re-run; it never overwrites config.ini or your data.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(id -un)"

echo "== Enabling I2C and installing system packages"
sudo raspi-config nonint do_i2c 0
sudo apt-get update
sudo apt-get install -y python3-venv python3-dev i2c-tools

echo "== Creating Python environment (slow on a Pi Zero, be patient)"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

[ -f config.ini ] || { cp config.example.ini config.ini; chmod 600 config.ini; }

echo "== Looking for the sensor on the I2C bus"
if sudo i2cdetect -y 1 | grep -qE '(^| )(76|77)( |$)'; then
  echo "   found it"
else
  echo "   NOT FOUND at 0x76/0x77 - check wiring (VIN->3V3, GND->GND, SCK/SCL->pin 5, SDI/SDA->pin 3)"
fi

echo "== Installing systemd service"
sudo tee /etc/systemd/system/bme-tracker.service >/dev/null <<EOF
[Unit]
Description=BME680/BME280 tracker
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/tracker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable bme-tracker
sudo systemctl restart bme-tracker

echo
echo "Done. Dashboard: http://$(hostname -I | awk '{print $1}'):8680/"
echo "Settings: $DIR/config.ini   Logs: journalctl -u bme-tracker -f"
