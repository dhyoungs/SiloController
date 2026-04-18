# Patrick Blackett Silo 2 Controller

A Raspberry Pi 5 application that drives a linear actuator opening and
closing the silo lid on a vessel, while recording MAVLink telemetry
from an ArduPilot autopilot over USB. Runs under systemd with a
Flask web UI on port **5000**, MQTT (localhost:1883), and an optional
Tk desktop GUI when a display is connected.

## Quick start

```bash
cd ~
git clone https://github.com/dhyoungs/SiloController.git
cd SiloController
sudo bash install.sh
```

The installer sets up OS packages (ffmpeg, mosquitto, picamera2, opencv),
creates a Python venv, installs the systemd service, enables it on
boot, and runs a self-test that confirms `/api/stats` is live.

Open the UI at `http://<pi-ip>:5000/`.

## Docs

| File | Who it's for |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Future Claude sessions — full architecture, protocols, design decisions |
| [SiloController_Documentation.docx](SiloController_Documentation.docx) | Human-readable operator manual |

## Hardware

- Raspberry Pi 5, Raspberry Pi OS (64-bit)
- Eletechsup DR25E01 bistable DPDT relay on GPIO 17
- Linak LA 121P00 12 V linear actuator
- Matek H743 ArduPilot flight controller via USB
- Optional Pi Camera Module (auto-detected)

## Run as a service

```bash
sudo systemctl status silocontroller
sudo journalctl -u silocontroller -f
```
