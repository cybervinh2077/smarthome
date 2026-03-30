#!/bin/bash
# Setup script for Jetson Nano (Ubuntu 18.04/20.04)
set -e

echo "=== SmartHome Setup for Jetson Nano ==="

# 1. System deps
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv mosquitto mosquitto-clients curl

# 2. Start mosquitto
sudo systemctl enable mosquitto
sudo systemctl start mosquitto

# 3. Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 4. Pull model (chạy nền)
echo "Pulling phi3:mini model..."
ollama pull phi3:mini

# 5. Python venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "=== Setup done ==="
echo "Run: source .venv/bin/activate && python services/dashboard.py"
