# Silo Controller — Remote API Guide

This document describes how to consume telemetry and control the Silo Controller
from a remote machine (e.g. a Claude Code application on a nearby Ubuntu host).

The Pi runs a Flask HTTP server on **port 5000** (all interfaces).

---

## Discovering the Pi

The Pi's IP is shown on its desktop wallpaper (top-left corner) and is also
returned by the API. If you know the hostname:

```bash
ping pitest.local          # mDNS — works if avahi is running
```

All examples below use `PI` as a placeholder — substitute the real IP or hostname.

---

## Polling: GET /api/telemetry

Returns a single JSON snapshot of all current platform telemetry.

```bash
curl http://PI:5000/api/telemetry
```

### Response

```json
{
  "lat": 50.8364217,
  "lon": -0.8734921,
  "alt_m": 3.42,
  "heading_deg": 127.3,
  "yaw_deg": 128.1,
  "groundspeed": 2.45,
  "pitch_deg": -1.23,
  "roll_deg": 4.56,
  "gps_fix": 3,
  "satellites": 12,
  "hdop": 0.95,
  "vdop": 1.42,
  "h_acc": 1.2,
  "v_acc": 2.1,
  "valid": true,
  "radio_rssi": 180,
  "mlrs_lq_pct": 98,
  "timestamp": "2026-04-12T03:11:00.123456+00:00"
}
```

### Field reference

| Field | Type | Unit | Description |
|---|---|---|---|
| `lat` | float | degrees | Latitude (WGS84, 7 d.p.) |
| `lon` | float | degrees | Longitude (WGS84, 7 d.p.) |
| `alt_m` | float | metres | Altitude MSL |
| `heading_deg` | float | degrees | Course over ground (0–360, from GPS) |
| `yaw_deg` | float | degrees | Magnetic heading from AHRS (0–360) |
| `groundspeed` | float | m/s | Speed over ground |
| `pitch_deg` | float | degrees | Pitch, positive = nose up |
| `roll_deg` | float | degrees | Roll, positive = starboard heel |
| `gps_fix` | int | — | 0=none, 2=2D, 3=3D, 4=DGPS, 5=RTK float, 6=RTK fixed |
| `satellites` | int | — | Visible satellite count |
| `hdop` | float | — | Horizontal dilution of precision (lower = better) |
| `vdop` | float | — | Vertical dilution of precision |
| `h_acc` | float | metres | Horizontal accuracy estimate |
| `v_acc` | float | metres | Vertical accuracy estimate |
| `valid` | bool | — | `true` once at least one GPS position has been received |
| `radio_rssi` | int/null | — | RF link RSSI (0–254, 255=invalid, null=unavailable) |
| `mlrs_lq_pct` | int/null | % | mLRS link quality (0–100, null=unavailable) |
| `timestamp` | string | ISO 8601 | Server UTC time when the response was generated |

---

## Streaming: GET /api/events (Server-Sent Events)

For continuous real-time data (5 Hz), use the SSE endpoint. This is the
recommended method — it avoids polling overhead and delivers telemetry as
soon as it arrives from the autopilot.

```bash
curl -N http://PI:5000/api/events
```

### Event types

Each SSE message has an `event:` name and a JSON `data:` payload.

#### `telemetry` (5 Hz)

Same fields as `/api/telemetry` plus a `stats` object with rolling
min/avg/max for key metrics.

#### `state`

Silo state changes: `{"state": "open"}` — values: `open`, `closed`,
`opening`, `closing`.

#### `recording`

CSV telemetry recording state: `{"active": true}`.

#### `cam_recording`

Camera recording state: `{"active": true}`.

#### `heartbeat` (every 5 s)

```json
{
  "sw_uptime_s": 3600,
  "hw_uptime_s": 86400,
  "utc": "14:30:22.456",
  "ip": "10.100.151.193"
}
```

### Python SSE client example

```python
import json
import requests

def stream_telemetry(pi_host: str, port: int = 5000):
    """Yield parsed telemetry dicts from the SSE stream."""
    url = f"http://{pi_host}:{port}/api/events"
    with requests.get(url, stream=True, timeout=10) as r:
        r.raise_for_status()
        event_type = None
        for line in r.iter_lines(decode_unicode=True):
            if line.startswith("event:"):
                event_type = line[7:].strip()
            elif line.startswith("data:") and event_type == "telemetry":
                yield json.loads(line[5:])
                event_type = None

# Usage:
for frame in stream_telemetry("10.100.151.193"):
    print(f"Speed: {frame['groundspeed']:.1f} m/s  "
          f"Pos: {frame['lat']:.6f}, {frame['lon']:.6f}  "
          f"Pitch: {frame['pitch_deg']:.1f}°  "
          f"Roll: {frame['roll_deg']:.1f}°")
```

---

## Other useful endpoints

### Silo control

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/open` | Open the silo lid |
| `POST` | `/api/close` | Close the silo lid |
| `GET` | `/api/status` | Current silo state + recording status + uptime |

### Full diagnostics

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/diagnostics` | Autopilot identity, GPS module info, EKF health, sensor status, radio link stats, battery, and full status log |

### Telemetry recording (CSV)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/record/start` | Start recording telemetry to CSV |
| `POST` | `/api/record/stop` | Stop recording |
| `GET` | `/api/logfiles` | List recorded CSV files |
| `GET` | `/api/logfiles/<name>/download` | Download a CSV file |

### System

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/system` | IP address, hardware + software uptime, UTC time |
| `POST` | `/api/restart` | Graceful restart (systemd respawns the service) |

---

## Notes

- **No authentication.** The API is intended for use on a local/private network.
  Do not expose port 5000 to the internet.
- **Units are metric.** Speed is m/s (multiply by 1.94384 for knots).
  Altitude is metres MSL. Angles are degrees.
- **`valid` field.** Will be `false` until the GPS has delivered at least one
  position fix after startup. Ignore telemetry when `valid` is `false`.
- **`gps_fix` values.** 3 (3D fix) is the minimum for reliable position data.
  Values 4–6 indicate augmented fixes (DGPS, RTK).
- **Rate limiting.** There is none. The SSE stream pushes at 5 Hz. Polling
  `/api/telemetry` faster than 5 Hz will return duplicate data.
- **Calibration.** Pitch and roll values have calibration offsets applied
  (set via the Configuration tab or `/api/config/calibration`). The values
  you receive are corrected — they read zero when the vessel is level.
