# SmartHome Server

Edge server for smart home system running on Jetson Nano.

## Stack
- Python 3
- Jetson Nano

## Structure

```
smarthome/
├── server/       # API server
├── services/     # Camera, sensor, AI inference...
├── config/       # System configuration
├── scripts/      # Setup, deploy, systemd scripts
└── docs/         # Documentation
```

## Getting Started

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
