"""
5 Hz CSV recorder — Sea Trials Data Recording.

Writes one row every 200 ms while recording is active.
Each session creates a new timestamped file in recordings/.

Filename
--------
PB<YYYYMMDD>_<HHMMSS>.csv   e.g. PB20260411_143022.csv

CSV columns
-----------
timestamp_iso      ISO-8601 UTC timestamp
timestamp_unix     UNIX epoch (float, ms precision)
lat                decimal degrees
lon                decimal degrees
alt_m              metres MSL
heading_deg        0–360
groundspeed_ms     m/s
pitch_deg          degrees (+nose-up)
roll_deg           degrees (+starboard heel)
pitch_rate_deg_s   °/s (from stats tracker)
roll_rate_deg_s    °/s
yaw_rate_deg_s     °/s
gps_fix            0=none 2=2D 3=3D 6=RTK
satellites         count
silo_state         open | closed | opening | closing
silo_event         '' normally; state name on transition tick
silo_source        '' normally; 'gui' | 'web' | 'mqtt' | 'unknown'
"""

import csv
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

RECORD_DIR = Path(__file__).parent.parent / "recordings"
SAMPLE_HZ  = 5
INTERVAL   = 1.0 / SAMPLE_HZ

COLUMNS = [
    "timestamp_iso", "timestamp_unix",
    "lat", "lon", "alt_m",
    "heading_deg", "groundspeed_ms",
    "pitch_deg", "roll_deg",
    "pitch_rate_deg_s", "roll_rate_deg_s", "yaw_rate_deg_s",
    "gps_fix", "satellites",
    "silo_state", "silo_event", "silo_source",
]


class Recorder:
    def __init__(self, telemetry, silo, stats=None):
        self._telem    = telemetry
        self._silo     = silo
        self._stats    = stats
        self._lock     = threading.Lock()
        self._active   = False
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[bool], None]] = []
        self._event_q: queue.SimpleQueue = queue.SimpleQueue()

        silo.add_event_listener(self._on_silo_event)

    # ── Public ───────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def start_recording(self, source: str = "unknown") -> dict:
        with self._lock:
            if self._active:
                return {"ok": False, "reason": "Already recording"}
            self._active = True
        logger.info("Recording started (by %s)", source)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="recorder"
        )
        self._thread.start()
        self._notify_listeners(True)
        return {"ok": True}

    def stop_recording(self, source: str = "unknown") -> dict:
        with self._lock:
            if not self._active:
                return {"ok": False, "reason": "Not recording"}
            self._active = False
        logger.info("Recording stopped (by %s)", source)
        self._notify_listeners(False)
        return {"ok": True}

    def status(self) -> dict:
        return {"recording": self.active}

    def add_listener(self, fn: Callable[[bool], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    # ── Class method: load recent CSV files ──────────────────────────────────

    @classmethod
    def load_recent(cls, max_age_s: float = 1800) -> list[dict]:
        """
        Scan RECORD_DIR for PB*.csv files modified within max_age_s seconds.
        Parse all matching files robustly (handles truncated last lines,
        corrupt rows, wrong encoding from unclean shutdown).

        Returns a flat list of row dicts sorted by timestamp_unix ascending.
        Only rows whose timestamp_unix falls within max_age_s are included.
        """
        RECORD_DIR.mkdir(exist_ok=True)
        cutoff = time.time() - max_age_s
        rows: list[dict] = []

        for path in sorted(RECORD_DIR.glob("PB*.csv")):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue

            rows.extend(_parse_csv(path, cutoff))

        rows.sort(key=lambda r: float(r.get("timestamp_unix", 0)))
        logger.info("Loaded %d historical rows from recordings/", len(rows))
        return rows

    # ── Silo event listener ──────────────────────────────────────────────────

    def _on_silo_event(self, state: str, source: str) -> None:
        self._event_q.put((state, source))

    # ── Recorder loop ────────────────────────────────────────────────────────

    def _run(self) -> None:
        RECORD_DIR.mkdir(exist_ok=True)
        fname = datetime.now().strftime("PB%Y%m%d_%H%M%S.csv")
        path  = RECORD_DIR / fname
        logger.info("Recording to %s", path)

        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            fh.flush()

            next_tick = time.monotonic()
            while True:
                with self._lock:
                    if not self._active:
                        break
                wait = next_tick - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                next_tick += INTERVAL

                # Drain silo events that arrived since last tick
                silo_event = silo_source = ""
                while not self._event_q.empty():
                    try:
                        silo_event, silo_source = self._event_q.get_nowait()
                    except queue.Empty:
                        break

                frame = self._telem.frame

                # Get rates from stats tracker if available
                pr = rr = yr = 0.0
                if self._stats is not None:
                    s = self._stats.get(frame)
                    cur = s.get("current", {})
                    pr  = cur.get("pitch_rate", 0.0)
                    rr  = cur.get("roll_rate",  0.0)
                    yr  = cur.get("yaw_rate",   0.0)

                writer.writerow({
                    "timestamp_iso":   datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    "timestamp_unix":  round(time.time(), 3),
                    "lat":             round(frame.lat, 7),
                    "lon":             round(frame.lon, 7),
                    "alt_m":           round(frame.alt_m, 2),
                    "heading_deg":     round(frame.heading_deg, 1),
                    "groundspeed_ms":  round(frame.groundspeed, 2),
                    "pitch_deg":       round(frame.pitch_deg, 2),
                    "roll_deg":        round(frame.roll_deg, 2),
                    "pitch_rate_deg_s": round(pr, 2),
                    "roll_rate_deg_s":  round(rr, 2),
                    "yaw_rate_deg_s":   round(yr, 2),
                    "gps_fix":         frame.gps_fix,
                    "satellites":      frame.satellites,
                    "silo_state":      self._silo.state,
                    "silo_event":      silo_event,
                    "silo_source":     silo_source,
                })
                fh.flush()

        logger.info("Recording saved: %s", path)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _notify_listeners(self, active: bool) -> None:
        with self._lock:
            fns = list(self._listeners)
        for fn in fns:
            try:
                fn(active)
            except Exception:
                logger.exception("Recorder listener error")


# ── CSV parsing helper ────────────────────────────────────────────────────────

def _parse_csv(path: Path, cutoff_unix: float) -> list[dict]:
    """
    Robustly parse a PB*.csv file.

    Handles:
    - Truncated last line (power-loss mid-write): skips rows with
      fewer fields than the header.
    - Encoding issues: falls back from UTF-8 to latin-1.
    - Missing/corrupt numeric fields: skipped silently.
    - Rows outside the time window: excluded.
    """
    rows: list[dict] = []

    for encoding in ("utf-8", "latin-1"):
        try:
            with open(path, newline="", encoding=encoding, errors="replace") as fh:
                content = fh.read()
            break
        except OSError as exc:
            logger.warning("Cannot read %s: %s", path, exc)
            return rows

    lines = content.splitlines()
    if len(lines) < 2:
        return rows

    try:
        reader = csv.DictReader(lines)
        fieldnames = reader.fieldnames or []
        n_fields = len(fieldnames)

        for row in reader:
            # Skip truncated rows (fewer values than headers)
            if len(row) < n_fields:
                continue
            # Skip rows where timestamp_unix is missing or non-numeric
            ts_raw = row.get("timestamp_unix", "")
            try:
                ts = float(ts_raw)
            except (ValueError, TypeError):
                continue
            if ts < cutoff_unix:
                continue
            rows.append(dict(row))
    except Exception as exc:
        logger.warning("Error parsing %s: %s", path, exc)

    return rows
