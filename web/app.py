"""
Flask web server — HTTP REST API + Server-Sent Events.

Endpoints
---------
GET  /                      Browser control panel
GET  /api/status            {state, recording, uptime_s}
POST /api/open              {ok, reason?}
POST /api/close             {ok, reason?}
POST /api/record/start      {ok, reason?}
POST /api/record/stop       {ok, reason?}
GET  /api/telemetry         Current TelemetryFrame as JSON
GET  /api/events            SSE stream — named events:
                              event: state      {state}
                              event: telemetry  {lat, lon, …, stats}
                              event: recording  {active}
                              event: heartbeat  {uptime_s}
"""

import csv
import json
import logging
import math
import os
import queue
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

logger = logging.getLogger(__name__)

app = Flask(__name__)

_silo      = None
_telem     = None
_recorder  = None
_cam       = None   # CameraRecorder (may be None if no camera)
_stats     = None
_start_t   = time.monotonic()

_sse_queues: list[queue.Queue] = []
_sse_lock   = threading.Lock()


def init(silo, telemetry, recorder, stats, cam=None) -> None:
    global _silo, _telem, _recorder, _cam, _stats, _start_t
    _silo     = silo
    _telem    = telemetry
    _recorder = recorder
    _cam      = cam
    _stats    = stats
    _start_t  = time.monotonic()

    _silo.add_listener(_on_silo_state)
    _recorder.add_listener(_on_recording_change)
    if _cam:
        _cam.add_listener(_on_cam_recording_change)

    threading.Thread(target=_telem_push_loop, daemon=True, name="sse-telem").start()
    threading.Thread(target=_heartbeat_loop,  daemon=True, name="sse-hb").start()


def run(host: str = "0.0.0.0", port: int = 5000) -> None:
    logger.info("Web interface on http://%s:%d", host, port)
    app.run(host=host, port=port, threaded=True, use_reloader=False)


# ── SSE helpers ──────────────────────────────────────────────────────────────

def _broadcast(event: str, data: dict) -> None:
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        for q in list(_sse_queues):
            q.put(payload)


def _on_silo_state(state: str) -> None:
    _broadcast("state", {"state": state})


def _on_recording_change(active: bool) -> None:
    _broadcast("recording", {"active": active})


def _on_cam_recording_change(active: bool) -> None:
    _broadcast("cam_recording", {"active": active})


def _telem_push_loop() -> None:
    while True:
        time.sleep(0.2)
        if _telem is None:
            continue
        f = _telem.frame
        s = _stats.get(f) if _stats else {}
        # Include key link-quality fields in the telemetry push
        d = _telem.diagnostics if _telem else {}
        _broadcast("telemetry", {
            "lat":         round(f.lat, 7),
            "lon":         round(f.lon, 7),
            "alt_m":       round(f.alt_m, 2),
            "heading_deg": round(f.heading_deg, 1),
            "yaw_deg":     round(f.yaw_deg, 1),
            "groundspeed": round(f.groundspeed, 2),
            "pitch_deg":   round(f.pitch_deg, 2),
            "roll_deg":    round(f.roll_deg, 2),
            "gps_fix":     f.gps_fix,
            "satellites":  f.satellites,
            "hdop":        round(f.hdop, 2),
            "vdop":        round(f.vdop, 2),
            "h_acc":       round(f.h_acc, 1),
            "v_acc":       round(f.v_acc, 1),
            "valid":       f.valid,
            "stats":       s,
            "radio_rssi":  d.get("radio_rssi"),
            "mlrs_lq_pct": d.get("mlrs_lq_pct"),
        })


def _heartbeat_loop() -> None:
    while True:
        time.sleep(5)
        hw_up = 0
        try:
            with open("/proc/uptime") as f:
                hw_up = int(float(f.read().split()[0]))
        except Exception:
            pass
        ip = "—"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        _broadcast("heartbeat", {
            "sw_uptime_s": int(time.monotonic() - _start_t),
            "hw_uptime_s": hw_up,
            "utc":         datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "ip":          ip,
        })


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/static/geo/<path:filename>")
def static_geo(filename):
    import pathlib
    geo_dir = pathlib.Path(__file__).parent / "static" / "geo"
    return send_file(str(geo_dir / filename))


@app.route("/api/status")
def status():
    return jsonify({
        **_silo.status(),
        **_recorder.status(),
        "uptime_s": int(time.monotonic() - _start_t),
    })


@app.route("/api/open", methods=["POST"])
def api_open():
    return jsonify(_silo.open(source="web"))


@app.route("/api/close", methods=["POST"])
def api_close():
    return jsonify(_silo.close(source="web"))


@app.route("/api/config/declare", methods=["POST"])
def api_declare_state():
    """Manually declare current physical silo state without moving anything."""
    data = request.get_json(silent=True) or {}
    is_open = data.get("open")
    if is_open is None:
        return jsonify({"ok": False, "reason": "Missing 'open' boolean"}), 400
    return jsonify(_silo.declare_state(bool(is_open), source="config-ui"))


@app.route("/api/config/relay_state")
def api_relay_state():
    return jsonify({
        "relay_open":  _silo.relay_open,
        "state":       _silo.state,
        "travel_time": _silo.travel_time,
    })


@app.route("/api/config/calibration", methods=["GET"])
def api_get_calibration():
    from telemetry.reader import get_calibration
    return jsonify(get_calibration())


@app.route("/api/config/calibration", methods=["POST"])
def api_set_calibration():
    """Set explicit pitch/roll offsets: {"pitch_offset": 1.5, "roll_offset": -0.8}"""
    from telemetry.reader import set_calibration
    data = request.get_json(silent=True) or {}
    try:
        pitch = float(data.get("pitch_offset", 0.0))
        roll  = float(data.get("roll_offset",  0.0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "Invalid offset values"}), 400
    result = set_calibration(pitch, roll)
    return jsonify({"ok": True, **result})


@app.route("/api/config/calibration/capture", methods=["POST"])
def api_capture_level():
    """Capture current pitch/roll as the level reference (zero point)."""
    from telemetry.reader import capture_level_calibration
    if _telem is None:
        return jsonify({"ok": False, "reason": "Telemetry not available"}), 503
    f = _telem.frame
    result = capture_level_calibration(f.pitch_deg, f.roll_deg)
    logger.info("Level captured: raw pitch=%.2f  raw roll=%.2f", f.pitch_deg, f.roll_deg)
    return jsonify({"ok": True, **result, "captured_pitch": round(f.pitch_deg, 2),
                    "captured_roll": round(f.roll_deg, 2)})


@app.route("/api/config/calibration/reset", methods=["POST"])
def api_reset_calibration():
    """Reset both offsets to zero."""
    from telemetry.reader import set_calibration
    result = set_calibration(0.0, 0.0)
    return jsonify({"ok": True, **result})


@app.route("/api/config/travel_time", methods=["GET"])
def api_get_travel_time():
    return jsonify({"travel_time": _silo.travel_time})


@app.route("/api/config/travel_time", methods=["POST"])
def api_set_travel_time():
    data = request.get_json(silent=True) or {}
    try:
        secs = float(data["travel_time"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "reason": "Missing or invalid 'travel_time'"}), 400
    return jsonify(_silo.set_travel_time(secs))


@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    return jsonify(_recorder.start_recording(source="web"))


@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    return jsonify(_recorder.stop_recording(source="web"))


@app.route("/api/telemetry")
def api_telemetry():
    f = _telem.frame
    return jsonify({
        "lat":         f.lat,          "lon":         f.lon,
        "alt_m":       f.alt_m,        "heading_deg": f.heading_deg,
        "yaw_deg":     f.yaw_deg,      "groundspeed": f.groundspeed,
        "pitch_deg":   f.pitch_deg,    "roll_deg":    f.roll_deg,
        "gps_fix":     f.gps_fix,      "satellites":  f.satellites,
        "valid":       f.valid,
    })


@app.route("/api/diagnostics")
def api_diagnostics():
    d = _telem.diagnostics if _telem else {}
    # Merge live GPS frame fields so the Diagnostics page can show fix/sats/hdop etc.
    if _telem:
        f = _telem.frame
        d["gps_fix"]    = f.gps_fix
        d["satellites"] = f.satellites
        d["hdop"]       = round(f.hdop, 2)
        d["vdop"]       = round(f.vdop, 2)
        d["h_acc"]      = round(f.h_acc, 1) if f.h_acc else None
        d["v_acc"]      = round(f.v_acc, 1) if f.v_acc else None
    return jsonify(d)


@app.route("/api/system")
def api_system():
    """Real IP, hardware uptime, software uptime, UTC."""
    hw_up = 0
    try:
        with open("/proc/uptime") as f:
            hw_up = int(float(f.read().split()[0]))
    except Exception:
        pass
    ip = "—"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return jsonify({
        "ip":          ip,
        "hw_uptime_s": hw_up,
        "sw_uptime_s": int(time.monotonic() - _start_t),
        "utc":         datetime.now(timezone.utc).strftime("%H:%M:%S"),
    })


@app.route("/api/gps/redetect", methods=["POST"])
def api_gps_redetect():
    """Re-request GPS parameters from ArduPilot (non-disruptive)."""
    if _telem and _telem._conn:
        conn = _telem._conn
        for p in _telem._GPS_PARAMS:
            try:
                conn.mav.param_request_read_send(
                    conn.target_system, conn.target_component,
                    p.encode(), -1,
                )
            except Exception:
                pass
        return jsonify({"ok": True, "msg": "GPS parameters refreshed"})
    return jsonify({"ok": False, "msg": "Not connected"}), 503


@app.route("/api/restart", methods=["POST"])
def api_restart():
    logger.info("Restart requested from web")
    # Exit cleanly after the response is sent — systemd (Restart=always) brings it back up
    def _do_exit():
        time.sleep(0.8)   # enough time for Flask to flush the response
        os.kill(os.getpid(), 15)   # SIGTERM → clean shutdown → systemd restarts
    threading.Thread(target=_do_exit, daemon=False).start()
    return jsonify({"ok": True})


@app.route("/api/history")
def api_history():
    interval = float(request.args.get("interval_s", 5.0))
    return jsonify(_stats.get_history(interval_s=interval) if _stats else [])


@app.route("/api/track")
def api_track():
    return jsonify(_stats.get_track() if _stats else [])


@app.route("/api/logfiles")
def api_logfiles():
    """List all recording files with quick metadata (no full parse)."""
    from telemetry.recorder import RECORD_DIR
    RECORD_DIR.mkdir(exist_ok=True)
    files = []
    for p in sorted(RECORD_DIR.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
            # Quick row count: count newlines minus header
            with open(p, "rb") as fh:
                rows = max(0, fh.read().count(b"\n") - 1)
            files.append({
                "name":     p.name,
                "size_b":   st.st_size,
                "size_str": _human_size(st.st_size),
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                                    .strftime("%Y-%m-%d %H:%M UTC"),
                "rows":     rows,
                "path":     str(p),
            })
        except Exception:
            pass
    return jsonify(files)


@app.route("/api/logfiles/<name>/summary")
def api_logfile_summary(name):
    """Full parse of one recording file — returns stats summary."""
    from telemetry.recorder import RECORD_DIR
    p = (RECORD_DIR / name).resolve()
    # Safety: must stay inside RECORD_DIR
    if not str(p).startswith(str(RECORD_DIR.resolve())):
        return jsonify({"error": "invalid path"}), 400
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        return jsonify(_summarise_csv(p))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/logfiles/<name>/download")
def api_logfile_download(name):
    """Serve raw CSV file for download."""
    from telemetry.recorder import RECORD_DIR
    p = (RECORD_DIR / name).resolve()
    if not str(p).startswith(str(RECORD_DIR.resolve())):
        return jsonify({"error": "invalid path"}), 400
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(p), as_attachment=True, download_name=name,
                     mimetype="text/csv")


# ── Logfile helpers ───────────────────────────────────────────────────────────

def _human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _summarise_csv(path: Path) -> dict:
    """Parse a recording CSV and return a rich summary dict."""
    rows = []
    for enc in ("utf-8", "latin-1"):
        try:
            with open(path, newline="", encoding=enc, errors="replace") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    try:
                        float(row.get("timestamp_unix", "x"))
                        rows.append(row)
                    except ValueError:
                        pass
            break
        except OSError:
            pass

    if not rows:
        return {"error": "no valid rows", "rows": 0}

    def fv(row, key, default=0.0):
        try:
            return float(row.get(key) or default)
        except (ValueError, TypeError):
            return default

    # Time bounds
    ts_vals = [fv(r, "timestamp_unix") for r in rows]
    t0, t1  = min(ts_vals), max(ts_vals)
    dur_s   = t1 - t0

    def fmt_ts(t):
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Numeric metrics to summarise
    METRICS = [
        ("groundspeed_ms",   "Speed",       lambda v: round(v * 1.94384, 2), "kt"),
        ("pitch_deg",        "Pitch",        abs,                             "°"),
        ("roll_deg",         "Roll",         abs,                             "°"),
        ("pitch_rate_deg_s", "Pitch Rate",   abs,                             "°/s"),
        ("roll_rate_deg_s",  "Roll Rate",    abs,                             "°/s"),
        ("yaw_rate_deg_s",   "Yaw Rate",     abs,                             "°/s"),
        ("alt_m",            "Altitude",     lambda v: round(v, 1),           "m"),
    ]

    metric_stats = {}
    for col, label, transform, unit in METRICS:
        vals = [transform(fv(r, col)) for r in rows
                if r.get(col) not in (None, "")]
        if vals:
            metric_stats[label] = {
                "unit":  unit,
                "min":   round(min(vals), 2),
                "max":   round(max(vals), 2),
                "avg":   round(sum(vals) / len(vals), 2),
            }

    # GPS bounds
    lats = [fv(r, "lat") for r in rows if fv(r, "lat") != 0]
    lons = [fv(r, "lon") for r in rows if fv(r, "lon") != 0]
    gps_bounds = {}
    if lats and lons:
        gps_bounds = {
            "lat_min": round(min(lats), 6), "lat_max": round(max(lats), 6),
            "lon_min": round(min(lons), 6), "lon_max": round(max(lons), 6),
        }

    # Track (downsampled to ≤200 points)
    track_full = [(fv(r, "lat"), fv(r, "lon")) for r in rows
                  if fv(r, "lat") != 0 or fv(r, "lon") != 0]
    step = max(1, len(track_full) // 200)
    track = track_full[::step]

    # Silo events
    silo_events = []
    for r in rows:
        ev = r.get("silo_event", "")
        if ev:
            silo_events.append({
                "time":   r.get("timestamp_iso", ""),
                "event":  ev,
                "source": r.get("silo_source", ""),
                "state":  r.get("silo_state", ""),
            })

    # GPS fix quality histogram
    fix_counts: dict[str, int] = {}
    fix_names = {0:"No Fix",1:"No Fix",2:"2D",3:"3D",4:"DGPS",5:"RTK Float",6:"RTK Fixed"}
    for r in rows:
        try:
            fn = fix_names.get(int(r.get("gps_fix", 0)), "?")
        except (ValueError, TypeError):
            fn = "?"
        fix_counts[fn] = fix_counts.get(fn, 0) + 1

    # Satellite count stats
    sat_vals = [int(r["satellites"]) for r in rows
                if r.get("satellites", "").isdigit()]

    return {
        "filename":    path.name,
        "size_str":    _human_size(path.stat().st_size),
        "rows":        len(rows),
        "start_time":  fmt_ts(t0),
        "end_time":    fmt_ts(t1),
        "duration_s":  round(dur_s),
        "duration_str": _fmt_dur(round(dur_s)),
        "metrics":     metric_stats,
        "gps_bounds":  gps_bounds,
        "track":       track,
        "silo_events": silo_events,
        "fix_counts":  fix_counts,
        "sat_min":     min(sat_vals) if sat_vals else None,
        "sat_max":     max(sat_vals) if sat_vals else None,
        "sat_avg":     round(sum(sat_vals)/len(sat_vals), 1) if sat_vals else None,
    }


def _fmt_dur(s: int) -> str:
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


# ── Stress test ──────────────────────────────────────────────────────────────

@app.route("/api/stress/start", methods=["POST"])
def api_stress_start():
    data = request.get_json(silent=True) or {}
    try:
        cycles  = int(data.get("cycles", 5))
        pause_s = float(data.get("pause_s", 3.0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "Invalid cycles or pause_s"}), 400
    return jsonify(_silo.start_stress_test(cycles, pause_s))


@app.route("/api/stress/stop", methods=["POST"])
def api_stress_stop():
    return jsonify(_silo.stop_stress_test())


@app.route("/api/stress/status")
def api_stress_status():
    return jsonify(_silo.stress_test_status())


# ── Camera ────────────────────────────────────────────────────────────────────

@app.route("/api/camera/status")
def api_camera_status():
    if _cam is None:
        return jsonify({"camera_detected": False, "recording": False, "current_file": None})
    return jsonify(_cam.status())


@app.route("/api/camera/stream")
def api_camera_stream():
    if _cam is None or not _cam._detected:
        return Response(status=204)
    return Response(
        _cam.stream_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/camera/snapshot")
def api_camera_snapshot():
    if _cam is None:
        return Response(status=204)
    jpeg = _cam.snapshot()
    if jpeg is None:
        return Response(status=204)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        jpeg,
        mimetype="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="snapshot_{ts}.jpg"'},
    )


@app.route("/api/camera/message", methods=["POST"])
def api_camera_message():
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()
    if not text:
        return jsonify({"ok": False, "reason": "Empty message"}), 400
    if _cam is None:
        return jsonify({"ok": True, "note": "no camera — message logged only"})
    return jsonify(_cam.post_message(text))


@app.route("/api/camera/messages")
def api_camera_messages():
    msgs = _cam.get_messages() if _cam else []
    return jsonify([{"ts": ts, "text": text} for ts, text in msgs])


@app.route("/api/camera/videos")
def api_camera_videos():
    if _cam is None:
        from camera.recorder import CameraRecorder
        return jsonify(CameraRecorder().list_videos())
    return jsonify(_cam.list_videos())


@app.route("/api/camera/videos/<name>/download")
def api_camera_video_download(name):
    from camera.recorder import VIDEO_DIR
    p = (VIDEO_DIR / name).resolve()
    if not str(p).startswith(str(VIDEO_DIR.resolve())):
        return jsonify({"error": "invalid path"}), 400
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(p), as_attachment=True, download_name=name,
                     mimetype="video/mp4")


@app.route("/api/camera/record/start", methods=["POST"])
def api_camera_record_start():
    if _cam is None:
        return jsonify({"ok": False, "reason": "No camera"}), 503
    return jsonify(_cam.start_recording())


@app.route("/api/camera/record/stop", methods=["POST"])
def api_camera_record_stop():
    if _cam is None:
        return jsonify({"ok": False, "reason": "No camera"}), 503
    return jsonify(_cam.stop_recording())


@app.route("/api/events")
def sse():
    client_q: queue.Queue = queue.Queue()
    with _sse_lock:
        _sse_queues.append(client_q)

    # Seed on connect
    client_q.put(f"event: state\ndata: {json.dumps(_silo.status())}\n\n")
    client_q.put(f"event: recording\ndata: {json.dumps(_recorder.status())}\n\n")
    client_q.put(f"event: heartbeat\ndata: {json.dumps({'sw_uptime_s': int(time.monotonic() - _start_t)})}\n\n")

    def generate():
        try:
            while True:
                try:
                    yield client_q.get(timeout=25)
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                if client_q in _sse_queues:
                    _sse_queues.remove(client_q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
