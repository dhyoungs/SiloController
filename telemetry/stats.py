"""
Motion statistics tracker — sea trials data.

Samples the TelemetryReader at 5 Hz and maintains a 30-minute rolling
buffer.  Computes peak values over 1 / 5 / 10 / 30-minute windows.

Metrics
-------
speed       knots          (groundspeed m/s → kts)
pitch       absolute °     (max amplitude — bow-up or bow-down)
pitch_rate  absolute °/s   (rate of change of pitch)
roll        absolute °     (max amplitude — port or starboard heel)
roll_rate   absolute °/s   (rate of change of roll)
yaw_rate    absolute °/s   (rate of turn)

Peaks always store the maximum absolute value seen in the window.
The "current" dict returns signed instantaneous values for pitch and
roll (so the display can show + = bow-up / starboard) but unsigned
rate values.

History / track
---------------
get_history(interval_s)  → list of dicts, downsampled to interval_s
get_track()              → list of (lat, lon) pairs (non-zero positions)

seed(rows)               → pre-populate the buffer from a list of CSV-
                           row dicts (from Recorder.load_recent).
"""

import math
import threading
import time
from collections import deque

KNOTS = 1.94384   # m/s → knots

WINDOWS = {
    "1m":  60,
    "5m":  300,
    "10m": 600,
    "30m": 1800,
}

# Buffer tuple indices
_I_TS          = 0
_I_SPEED       = 1
_I_PITCH       = 2   # signed
_I_ABS_PITCH   = 3
_I_PITCH_RATE  = 4
_I_ROLL        = 5   # signed
_I_ABS_ROLL    = 6
_I_ROLL_RATE   = 7
_I_YAW_RATE    = 8
_I_LAT         = 9
_I_LON         = 10
_I_HEADING     = 11
_I_ALT         = 12


class StatsTracker:
    def __init__(self, telemetry):
        self._telem = telemetry
        self._lock  = threading.Lock()

        # Each entry: (ts, speed_kts, pitch_deg, abs_pitch, pitch_rate,
        #              roll_deg, abs_roll, roll_rate, yaw_rate,
        #              lat, lon, heading_deg, alt_m)
        self._buf: deque = deque()

        self._prev_pitch = None
        self._prev_roll  = None
        self._prev_yaw   = None
        self._prev_ts    = None

        # Latest instantaneous rates (unsigned)
        self._cur_pitch_rate = 0.0
        self._cur_roll_rate  = 0.0
        self._cur_yaw_rate   = 0.0

        threading.Thread(target=self._loop, daemon=True, name="stats-feed").start()

    # ── Public ───────────────────────────────────────────────────────────────

    def get(self, frame) -> dict:
        """
        Returns a stats dict with 'current' and one entry per peak window.

        current keys : speed, pitch, pitch_rate, roll, roll_rate, yaw_rate
        peak keys    : speed, pitch, pitch_rate, roll, roll_rate, yaw_rate
        """
        with self._lock:
            pr = self._cur_pitch_rate
            rr = self._cur_roll_rate
            yr = self._cur_yaw_rate

        speed = frame.groundspeed * KNOTS
        return {
            "current": {
                "speed":      round(speed,           1),
                "pitch":      round(frame.pitch_deg, 1),
                "pitch_rate": round(pr,              1),
                "roll":       round(frame.roll_deg,  1),
                "roll_rate":  round(rr,              1),
                "yaw_rate":   round(yr,              1),
            },
            **{name: self._window_stats(secs) for name, secs in WINDOWS.items()},
        }

    def seed(self, rows: list[dict]) -> None:
        """
        Pre-populate the rolling buffer from a list of CSV row dicts
        (as returned by Recorder.load_recent).

        Rows are expected to have keys: timestamp_unix, lat, lon, alt_m,
        heading_deg, groundspeed_ms, pitch_deg, roll_deg.
        pitch_rate_deg_s / roll_rate_deg_s / yaw_rate_deg_s are used if
        present; otherwise computed from successive rows.

        Only rows within the last 30 minutes are kept.
        """
        if not rows:
            return

        now    = time.monotonic()
        # We need a reference to convert UNIX times → monotonic
        wall_now = time.time()
        cutoff_unix = wall_now - 1800

        # Filter and sort
        valid = [r for r in rows if _float(r.get("timestamp_unix", 0)) >= cutoff_unix]
        valid.sort(key=lambda r: _float(r["timestamp_unix"]))

        entries = []
        prev_pitch = prev_roll = prev_yaw = None
        prev_unix  = None

        for r in valid:
            unix = _float(r["timestamp_unix"])
            ts   = now - (wall_now - unix)   # monotonic equivalent

            pitch = _float(r.get("pitch_deg", 0))
            roll  = _float(r.get("roll_deg",  0))
            # yaw_deg may not be in old CSVs — use heading as fallback
            yaw   = _float(r.get("yaw_deg", r.get("heading_deg", 0)))
            speed = _float(r.get("groundspeed_ms", 0)) * KNOTS

            # Rates: use recorded if available, else compute from delta
            if "pitch_rate_deg_s" in r and r["pitch_rate_deg_s"] != "":
                pr = abs(_float(r["pitch_rate_deg_s"]))
                rr = abs(_float(r.get("roll_rate_deg_s", 0)))
                yr = abs(_float(r.get("yaw_rate_deg_s",  0)))
            elif prev_unix is not None:
                dt = unix - prev_unix
                if dt > 0:
                    pr = abs((pitch - prev_pitch) / dt)
                    rr = abs((roll  - prev_roll)  / dt)
                    dy = yaw - prev_yaw
                    while dy >  180: dy -= 360
                    while dy < -180: dy += 360
                    yr = abs(dy / dt)
                else:
                    pr = rr = yr = 0.0
            else:
                pr = rr = yr = 0.0

            entries.append((
                ts,
                speed,
                pitch, abs(pitch), pr,
                roll,  abs(roll),  rr,
                yr,
                _float(r.get("lat", 0)),
                _float(r.get("lon", 0)),
                _float(r.get("heading_deg", 0)),
                _float(r.get("alt_m", 0)),
            ))

            prev_pitch = pitch
            prev_roll  = roll
            prev_yaw   = yaw
            prev_unix  = unix

        with self._lock:
            # Merge: existing live entries are newer; keep them
            for e in entries:
                self._buf.appendleft(e)
            # Re-sort by timestamp and trim to 30 min
            sorted_buf = sorted(self._buf, key=lambda x: x[_I_TS])
            cutoff_ts  = now - 1800
            self._buf  = deque(e for e in sorted_buf if e[_I_TS] >= cutoff_ts)

    def get_history(self, interval_s: float = 5.0) -> list[dict]:
        """
        Return a downsampled view of the last 30 minutes.

        Each entry is a dict with keys:
          ts_unix, lat, lon, alt_m, heading_deg, speed_kts,
          pitch_deg, roll_deg, pitch_rate, roll_rate, yaw_rate
        """
        now      = time.monotonic()
        wall_now = time.time()

        with self._lock:
            buf = list(self._buf)

        if not buf:
            return []

        result   = []
        last_ts  = None

        for row in buf:
            ts = row[_I_TS]
            if last_ts is None or (ts - last_ts) >= interval_s:
                unix = wall_now - (now - ts)
                result.append({
                    "ts_unix":     round(unix, 3),
                    "lat":         row[_I_LAT],
                    "lon":         row[_I_LON],
                    "alt_m":       round(row[_I_ALT], 2),
                    "heading_deg": round(row[_I_HEADING], 1),
                    "speed_kts":   round(row[_I_SPEED], 2),
                    "pitch_deg":   round(row[_I_PITCH], 2),
                    "roll_deg":    round(row[_I_ROLL], 2),
                    "pitch_rate":  round(row[_I_PITCH_RATE], 2),
                    "roll_rate":   round(row[_I_ROLL_RATE], 2),
                    "yaw_rate":    round(row[_I_YAW_RATE], 2),
                })
                last_ts = ts

        return result

    def get_track(self) -> list[tuple[float, float]]:
        """Return list of (lat, lon) pairs for all valid positions in buffer."""
        with self._lock:
            buf = list(self._buf)
        return [
            (row[_I_LAT], row[_I_LON])
            for row in buf
            if row[_I_LAT] != 0.0 or row[_I_LON] != 0.0
        ]

    # ── Internal ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            time.sleep(0.2)   # 5 Hz
            f  = self._telem.frame
            ts = time.monotonic()

            if self._prev_ts is not None:
                dt = ts - self._prev_ts
                if dt > 0:
                    pr = abs((f.pitch_deg - self._prev_pitch) / dt)
                    rr = abs((f.roll_deg  - self._prev_roll)  / dt)
                    dy = f.yaw_deg - self._prev_yaw
                    while dy >  180: dy -= 360
                    while dy < -180: dy += 360
                    yr = abs(dy / dt)
                else:
                    pr = rr = yr = 0.0
            else:
                pr = rr = yr = 0.0

            self._prev_pitch = f.pitch_deg
            self._prev_roll  = f.roll_deg
            self._prev_yaw   = f.yaw_deg
            self._prev_ts    = ts

            speed_kts = f.groundspeed * KNOTS

            with self._lock:
                self._cur_pitch_rate = pr
                self._cur_roll_rate  = rr
                self._cur_yaw_rate   = yr
                self._buf.append((
                    ts,
                    speed_kts,
                    f.pitch_deg, abs(f.pitch_deg), pr,
                    f.roll_deg,  abs(f.roll_deg),  rr,
                    yr,
                    f.lat, f.lon, f.heading_deg, f.alt_m,
                ))
                cutoff = ts - 1800
                while self._buf and self._buf[0][_I_TS] < cutoff:
                    self._buf.popleft()

    def _window_stats(self, seconds: int) -> dict:
        """Return {metric: {min, max, avg}} for all metrics in the time window."""
        cutoff = time.monotonic() - seconds
        with self._lock:
            rows = [r for r in self._buf if r[_I_TS] >= cutoff]
        if not rows:
            zero = {"min": 0.0, "max": 0.0, "avg": 0.0}
            return {k: dict(zero) for k in
                    ("speed", "pitch", "pitch_rate", "roll", "roll_rate", "yaw_rate")}

        def _s(vals):
            return {
                "min": round(min(vals), 1),
                "max": round(max(vals), 1),
                "avg": round(sum(vals) / len(vals), 1),
            }

        return {
            "speed":      _s([r[_I_SPEED]      for r in rows]),
            "pitch":      _s([r[_I_ABS_PITCH]  for r in rows]),
            "pitch_rate": _s([r[_I_PITCH_RATE] for r in rows]),
            "roll":       _s([r[_I_ABS_ROLL]   for r in rows]),
            "roll_rate":  _s([r[_I_ROLL_RATE]  for r in rows]),
            "yaw_rate":   _s([r[_I_YAW_RATE]   for r in rows]),
        }


def _float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
