# Bambu Lab P1S Monitor

A simple Python CLI tool that connects to a Bambu Lab P1S printer over MQTT and streams live status to the terminal.

## Features

- Real-time print progress with progress bar, layer info, and ETA
- Nozzle, bed, and chamber temperature display
- HMS error/warning codes with human-readable descriptions
- Color-coded terminal output
- Graceful Ctrl+C disconnect

## Requirements

- Python 3.8+
- Bambu Lab P1S printer in LAN-only dev mode
- Printer's IP address, serial number, and LAN access code

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your printer's details:

```
BAMBU_IP=192.168.1.100
BAMBU_SERIAL=your_serial_number
BAMBU_ACCESS_CODE=your_lan_access_code
```

You can find the LAN access code in the printer's settings under **Network > LAN Only Mode**.

## Usage

```bash
python bambu_monitor.py
```

The monitor connects to the printer's MQTT broker over TLS (port 8883), requests a full status dump, then continuously displays updates as they arrive.

## How It Works

The printer exposes an MQTT broker with TLS on port 8883. The monitor authenticates with username `bblp` and the LAN access code, subscribes to `device/{SERIAL}/report`, and sends a `pushall` command to get the initial state. Subsequent updates are pushed by the printer automatically during prints.
