# CLAUDE.md — Silo Controller

This file gives Claude Code full context to understand, extend, and maintain
the Silo Controller project without needing prior conversation history.

---

## Project overview

**Patrick Blackett Silo 2 Controller** — a Raspberry Pi 5 application that
controls a linear actuator opening/closing a silo lid on a vessel, while
recording flight/navigation telemetry from an ArduPilot autopilot over MAVLink.

Runs headless under systemd (web + MQTT), with an optional Tk desktop GUI when
a display is connected. All control is available via web browser, MQTT, or HTTP
REST API.

---

## Hardware

| Item | Detail |
|---|---|
| Compute | Raspberry Pi 5 |
| Relay | Eletechsup DR25E01 — 3 A, 1-ch, **bistable DPDT toggle relay** |
| Actuator | Linak LA 121P00-1101220, 12 V DC linear actuator (internal end-stop limit switches) |
| Autopilot/FC | Matek H743 running ArduPilot, connected via USB serial |
| GPS | u-blox M10 via ArduPilot (GPS1_TYPE=9) |
| RF link | mLRS (MAVLink over radio link) |
| Camera | Pi Camera Module (optional — auto-detected) |

### GPIO wiring (BCM numbering)

| Pi pin | Relay pin | Function |
|---|---|---|
| GPIO 17 | DR25E01 T | Trigger — 100 ms LOW pulse toggles relay |
| 5 V | DR25E01 V | Coil power |
| GND | DR25E01 G | Ground |

Relay OUT+/OUT− → Actuator motor terminals.
12 V PSU → Relay motor power input (separate from logic).

### How the bistable relay works

The relay toggles on each LOW pulse on T, then **latches** without power.
Software tracks which position the relay is in (`relay_open` bool) and only
pulses when a direction change is needed.  State is persisted to
`silo_state.json` and survives reboots.

If open/close direction is backwards, swap OUT+/OUT− on the actuator terminals.

---

## Architecture

```
main.py
  ├── core/silo.py          SiloController   — state machine + GPIO driver
  ├── telemetry/reader.py   TelemetryReader  — MAVLink / serial reader
  ├── telemetry/recorder.py Recorder         — CSV telemetry logger
  ├── telemetry/stats.py    StatsTracker     — rolling min/avg/max
  ├── camera/recorder.py    CameraRecorder   — Pi camera, MJPEG, MP4 recording
  ├── web/app.py            Flask REST API   — port 5000
  ├── mqtt/handler.py       MqttHandler      — Mosquitto broker localhost:1883
  └── gui/app.py            SiloGUI          — optional tkinter desktop UI
```

All subsystems run on daemon threads.  `main()` blocks on the Tk event loop
(or `signal.pause()` when headless).

---

## Key files

### `core/silo.py`
- `SiloController` — thread-safe state machine: `closed → opening → open → closing → closed`
- `PIN_TRIGGER = 17`, `PULSE_MS = 100`, `DEFAULT_TRAVEL_TIME = 2.0`
- `STATE_FILE = silo_state.json` in project root — persists `relay_open` + `travel_time`
- `open(source)` / `close(source)` — trigger actuator; source string logged/published
- `declare_state(is_open)` — declare physical position without moving (config UI)
- `set_travel_time(seconds)` — clamp 0.5–120 s, persist
- `start_stress_test(cycles, pause_s)` / `stop_stress_test()` / `stress_test_status()`
- Two listener types: `add_listener(fn(state))` and `add_event_listener(fn(state, source))`
- Uses `gpiozero OutputDevice`; falls back to simulation if GPIO unavailable

### `telemetry/reader.py`
- `TelemetryReader` — connects to MAVLink device (auto-detects `/dev/ttyACM*`, `/dev/ttyUSB*`)
- `frame` property → `TelemetryFrame(lat, lon, alt_m, heading_deg, groundspeed, pitch_deg, roll_deg, yaw_deg, gps_fix, satellites, hdop, vdop, h_acc, v_acc, valid)`
- `diagnostics` property → full dict of autopilot, GPS, EKF, radio, battery data
- GPS fix/sats/hdop/vdop live in `_frame`, not `_diag` — `/api/diagnostics` merges them in
- `_GPS_PARAMS` — param set fetched on connect, handled in `_ingest()` (not a separate thread)
- `GPS_STATUS` messages populate `satellites_info` list for the sky plot
- STATUSTEXT captures GPS module version strings (ArduPilot only sends these at boot)

### `camera/recorder.py`
- `CameraRecorder` — auto-detects Pi camera via `Picamera2.global_camera_info()`
- Graceful stub when no camera or picamera2/cv2 not installed
- Records H264 MP4 to `recordings/video/PBvideo_YYYYMMDD_HHMMSS.mp4`
- **Always-on telem overlay** on both MJPEG stream and recorded video: UTC time,
  speed (kt), alt (m), pitch, roll, COG, GPS fix/sats, last 3 custom messages
- Recording starts on silo `opening` event; stops 2 s after `closed`
- `on_silo_event(state, source)` auto-posts timestamped event messages
- Rolling disk buffer: deletes oldest `.mp4` when disk free < 15%
- `post_message(text)` — stores msg, shown on overlay (last 3) and Video tab log
- MJPEG stream at `/api/camera/stream` (multipart/x-mixed-replace)

### `web/app.py`
- Flask on port 5000, threaded, no reloader
- SSE at `/api/events` — pushes `state`, `telemetry`, `recording`, `cam_recording`, `heartbeat`
- `init(silo, telem, recorder, stats, cam=None)` — wire up all subsystems
- GPS frame data merged into `/api/diagnostics` response (fix, sats, hdop, vdop, h_acc, v_acc)

### `mqtt/handler.py`
- Subscribe: `silo/command` (open/close/status), `silo/record` (start/stop/status), `silo/message` (camera overlay text)
- Publish: `silo/status` (retained), `silo/recording` (retained), `silo/event` (non-retained JSON)
- `silo/event` payload: `{"event":"opening","ts":"2026-04-12T03:11:00Z","source":"web"}`

### `web/templates/index.html`
- Single-page app; tabs: **Live | Video | History | Diagnostics | Log Files | Configuration | API Docs**
- SSE-driven updates at 5 Hz telemetry push
- Video tab: MJPEG live feed (military test-range aesthetic), telem panel outside image, custom message log, snapshot/record controls, saved video list
- Configuration tab: declare silo position, travel time, stress test (cycles + pause, progress bar)
- API Docs tab: curl + mosquitto commands with real Pi IP pre-filled, copy buttons

---

## REST API endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/open` | Open silo |
| POST | `/api/close` | Close silo |
| GET | `/api/status` | Silo state + recording + uptime |
| GET | `/api/telemetry` | Live telemetry frame |
| GET | `/api/diagnostics` | Full autopilot diagnostics (includes GPS frame fields) |
| GET | `/api/events` | SSE stream |
| POST | `/api/record/start` | Start CSV recording |
| POST | `/api/record/stop` | Stop CSV recording |
| POST | `/api/config/declare` | Declare silo position `{"open": true}` |
| GET/POST | `/api/config/travel_time` | Get/set actuator travel time |
| GET | `/api/config/relay_state` | Relay open bool + state + travel_time |
| POST | `/api/stress/start` | `{"cycles":10,"pause_s":3}` |
| POST | `/api/stress/stop` | Abort stress test |
| GET | `/api/stress/status` | `{running, done, total, step}` |
| GET | `/api/camera/status` | Camera detected + recording state |
| GET | `/api/camera/stream` | MJPEG live stream |
| GET | `/api/camera/snapshot` | JPEG download |
| POST | `/api/camera/message` | `{"message":"text"}` — overlay annotation |
| GET | `/api/camera/messages` | All messages log |
| GET | `/api/camera/videos` | List saved MP4s |
| GET | `/api/camera/videos/<name>/download` | Download MP4 |
| POST | `/api/camera/record/start` | Manual record start |
| POST | `/api/camera/record/stop` | Manual record stop |
| POST | `/api/gps/redetect` | Re-request GPS params (non-disruptive) |
| POST | `/api/restart` | Graceful restart (systemd respawns) |

---

## MQTT topics

| Direction | Topic | Payload |
|---|---|---|
| Subscribe | `silo/command` | `OPEN` / `CLOSE` / `STATUS` |
| Subscribe | `silo/record` | `START` / `STOP` / `STATUS` |
| Subscribe | `silo/message` | Any text → camera overlay |
| Publish (retained) | `silo/status` | `open` / `closed` / `opening` / `closing` |
| Publish (retained) | `silo/recording` | `true` / `false` |
| Publish | `silo/event` | JSON `{"event":…,"ts":…,"source":…}` |

---

## Persistent state

`silo_state.json` (project root):
```json
{
  "relay_open": false,
  "travel_time": 10.0
}
```
Updated on every relay toggle and manual declaration.  Survives reboots.

---

## Recordings

- **CSV telemetry**: `recordings/PB*.csv` — one file per recording session
- **MP4 video**: `recordings/video/PBvideo_*.mp4` — H264, 1280×720, 30 fps
- Disk guard: oldest MP4 deleted if disk free < 15%

---

## GPS assistance (tools/gps_assist.py)

Injects cold-start assistance to the u-blox GPS via MAVLink GPS_INJECT_DATA:
1. UTC time (from Pi NTP-synced clock)
2. Approximate position (default: PO18 9AB, Bosham/Chichester)
3. YUMA almanac from NAVCEN USCG (no registration required)

Run: `python tools/gps_assist.py [--port /dev/ttyACM1] [--lat 50.83 --lon -0.87]`

---

## Dependencies

```
flask>=3.0
paho-mqtt>=1.6
gpiozero>=2.0
pymavlink>=2.4
picamera2          # optional — Pi camera recording
opencv-python      # optional — telemetry overlay
```

System packages:
```
sudo apt install mosquitto mosquitto-clients ffmpeg
```

---

## Running as a service

```ini
# /etc/systemd/system/silo.service
[Unit]
Description=Silo Controller
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/SiloController/main.py
WorkingDirectory=/home/pi/SiloController
Restart=always
RestartSec=3
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now silo
sudo journalctl -u silo -f
```

---

## Known design decisions

- **Bistable relay tracking**: relay has no feedback output; `relay_open` bool tracks
  current position in memory and persisted JSON.  Use the Configuration tab to
  re-sync if the physical state ever drifts.
- **GPS diagnostics merge**: `gps_fix`, `satellites`, `hdop`, `vdop` live in the
  telemetry frame (`_frame`), not the diagnostics dict.  The `/api/diagnostics`
  endpoint merges them for the web UI.
- **PARAM_VALUE race condition fix**: GPS params are requested on connect and handled
  in `_ingest()` alongside all other messages — no separate fetch thread.
- **Camera overlay on both streams**: `_build_overlay_lines()` is shared between the
  pre_callback (burns into MP4) and `_capture_loop` (drawn on MJPEG JPEG frames).
- **Stress test**: waits for actual state transitions (polls `silo.state`) rather than
  sleeping for travel_time — catches real hardware timing.
