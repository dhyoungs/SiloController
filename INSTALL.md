# Silo Controller — Installation Guide

## Hardware

| Component | Detail |
|---|---|
| Compute | Raspberry Pi 5 |
| Relay | Eletechsup DR25E01 — 3 A bistable DPDT toggle relay |
| Actuator | Linak LA 121P00-1101220, 12 V DC linear actuator |
| Autopilot | Matek H743 running ArduPilot, USB serial |
| Camera | Pi Camera Module (optional) |

**GPIO wiring (BCM)**

| Pi pin | Relay pin | Function |
|---|---|---|
| GPIO 17 | T | Trigger — 100 ms LOW pulse toggles relay |
| 5 V | V | Coil power |
| GND | G | Ground |

---

## Quick Start

1. Flash **Raspberry Pi OS (64-bit)** to an SD card using Raspberry Pi Imager.
   Set hostname, enable SSH, and set username to `pi`.

2. Boot the Pi, open a terminal (SSH or local), and copy the zip to it:

   ```bash
   scp SiloController_install.zip pi@<pi-ip>:~
   ```

3. On the Pi, extract and run the installer:

   ```bash
   unzip SiloController_install.zip
   cd SiloController_install
   sudo bash install.sh
   ```

4. When the script finishes it prints the web UI address. Open it in a browser:

   ```
   http://<pi-ip>:5000
   ```

5. If the camera or GPIO are not detected on first boot, reboot the Pi:

   ```bash
   sudo reboot
   ```

---

## What the Installer Does

| Step | Action |
|---|---|
| 1 | `apt install` — ffmpeg, mosquitto, python3-picamera2, python3-opencv |
| 2 | Copies application code to `/home/pi/SiloController` |
| 3 | Creates Python venv at `.venv` with `--system-site-packages` |
| 4 | `pip install` — flask, paho-mqtt, gpiozero, pymavlink, numpy |
| 5 | Writes `/etc/mosquitto/conf.d/silo.conf` (anonymous local listener) |
| 6 | Writes and enables `/etc/systemd/system/silocontroller.service` |
| 7 | Adds `pi` user to `gpio` group |
| 8 | Creates "Skopa Silo Controller" desktop icon (if desktop present) |
| 9 | Installs network overlay timer (shows IP on wallpaper) |
| 10 | Starts mosquitto and silocontroller immediately |

---

## Service Management

```bash
# View live logs
journalctl -u silocontroller -f

# Restart the service
sudo systemctl restart silocontroller

# Stop / start
sudo systemctl stop silocontroller
sudo systemctl start silocontroller

# Disable autostart
sudo systemctl disable silocontroller
```

---

## Web Interface — Tabs

| Tab | Description |
|---|---|
| **Live** | Real-time telemetry, silo open/close controls, recording |
| **Video** | MJPEG live stream, snapshot, saved video list with download |
| **History** | CSV telemetry recordings with download |
| **Diagnostics** | Full autopilot / GPS / EKF / radio diagnostics |
| **Log Files** | Application log viewer |
| **Configuration** | Travel time, declare silo position, actuator stress test |
| **API Docs** | curl and mosquitto examples with your Pi's IP pre-filled |

---

## REST API Quick Reference

```bash
PI=<pi-ip>

# Open / close silo
curl -X POST http://$PI:5000/api/open
curl -X POST http://$PI:5000/api/close

# Check status
curl http://$PI:5000/api/status

# Start / stop CSV telemetry recording
curl -X POST http://$PI:5000/api/record/start
curl -X POST http://$PI:5000/api/record/stop

# Start / stop video recording
curl -X POST http://$PI:5000/api/camera/record/start
curl -X POST http://$PI:5000/api/camera/record/stop

# Live telemetry
curl http://$PI:5000/api/telemetry
```

---

## MQTT Topics

```bash
# Open / close via MQTT
mosquitto_pub -h <pi-ip> -t silo/command -m OPEN
mosquitto_pub -h <pi-ip> -t silo/command -m CLOSE

# Subscribe to state changes
mosquitto_sub -h <pi-ip> -t silo/status
mosquitto_sub -h <pi-ip> -t silo/event
```

---

## Configuration

### Silo travel time

Set how long the actuator takes to fully open or close. Default is 10 s.
Use the **Configuration** tab in the web UI, or:

```bash
curl -X POST http://$PI:5000/api/config/travel_time \
     -H "Content-Type: application/json" \
     -d '{"travel_time": 12.5}'
```

### Declare physical position

If the tracked state ever drifts from the physical reality, use the
**Configuration** tab → "Declare Position", or:

```bash
curl -X POST http://$PI:5000/api/config/declare \
     -H "Content-Type: application/json" \
     -d '{"open": false}'
```

---

## Persistent Files

| File | Purpose |
|---|---|
| `silo_state.json` | Relay position and travel time — survives reboots |
| `calibration.json` | Pitch/roll offsets |
| `recordings/*.csv` | Telemetry recordings |
| `recordings/video/*.mp4` | Video recordings (1280×720 H264, 30 fps) |
| `logs/api_messages.jsonl` | Persistent log of all inbound API actions |

---

## Re-installing / Updating

Run `install.sh` again at any time. It is idempotent — it skips steps that are
already complete and does not overwrite `silo_state.json` or `calibration.json`
if they exist.

To force a clean reinstall, delete the target directory first:

```bash
sudo systemctl stop silocontroller
sudo rm -rf /home/pi/SiloController
sudo bash install.sh
```

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Web UI not reachable | `sudo systemctl status silocontroller` |
| GPIO not working | Reboot; check `groups pi` includes `gpio` |
| Camera not detected | Reboot; run `libcamera-hello` to verify camera |
| No MAVLink data | Check USB cable; `ls /dev/ttyACM*` |
| MQTT not connecting | `sudo systemctl status mosquitto` |
