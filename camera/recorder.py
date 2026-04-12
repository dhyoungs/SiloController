"""
Pi Camera recorder — auto-detection, H264 MP4 recording, MJPEG stream.

Design
------
* Detects a Pi camera on startup via Picamera2.global_camera_info().
* If no camera is found (or picamera2/cv2 unavailable) the class works in
  stub mode — all API calls return gracefully so the rest of the app is
  unaffected.
* Recording starts when the silo begins OPENING and stops 2 s after CLOSED.
* Telemetry bar (black strip below the camera image) is burned into both the
  MJPEG live stream and the recorded video — no video content is obscured.
  - Left panel: UTC time, speed, altitude, pitch, roll, COG, GPS fix/sats,
    latitude, longitude, last 3 custom messages.
  - Right panel: 60-second pitch/roll graph (pitch=cyan, roll=yellow).
* Recording uses raw BGR frames piped directly to FFmpeg (libx264), avoiding
  the picamera2 H264Encoder / FfmpegOutput prctl-SIGKILL thread bug.
* Rolling disk buffer: deletes oldest .mp4 when disk free < DISK_FREE_MIN_PCT.
* Custom messages are accepted via post_message(); the last three appear in
  the burned-in telemetry bar.
"""

import collections
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

VIDEO_DIR         = Path(__file__).parent.parent / "recordings" / "video"
DISK_FREE_MIN_PCT = 15.0
STREAM_FPS        = 20        # MJPEG / recording frame rate
HISTORY_S         = 60        # seconds of pitch/roll history for graph
RECORD_CRF        = 23        # libx264 CRF (lower = better quality)

# Output frame dimensions
CAM_W, CAM_H = 640, 360       # lores stream (source for both paths)
BAR_H         = 200           # telemetry bar height
FRAME_W       = CAM_W         # 640
FRAME_H       = CAM_H + BAR_H # 560

GRAPH_X       = 340           # x offset where the graph panel starts in the bar
GRAPH_W       = FRAME_W - GRAPH_X   # 300 px

# ── Optional deps ─────────────────────────────────────────────────────────────
try:
    from picamera2 import Picamera2
    _PIC2_OK = True
except Exception as _e:
    _PIC2_OK = False
    logger.info("picamera2 not available (%s) — camera disabled", _e)

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False


def _human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


class CameraRecorder:
    """
    Thread-safe Pi camera controller.

    Public API
    ----------
    status()                       → dict
    start_recording()              → dict
    stop_recording()               → dict
    snapshot()                     → bytes | None   (JPEG)
    stream_generator()             → iterator of MJPEG chunks
    post_message(text)             → dict
    get_messages()                 → list[tuple[str, str]]
    list_videos()                  → list[dict]
    set_telemetry(telem_reader)    → None
    on_silo_event(state, source)   → None  (silo event listener)
    add_listener(fn)               → None  fn(recording: bool)
    cleanup()                      → None
    """

    def __init__(self):
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)

        self._lock         = threading.Lock()
        self._cam          = None
        self._detected     = False
        self._recording    = False
        self._current_file = None
        self._rec_proc     = None    # FFmpeg subprocess for recording
        self._telem_ref    = None
        self._messages     : list[tuple[str, str]] = []   # (ts_str, text)
        self._latest_frame : bytes | None = None
        self._listeners    : list = []
        self._stop_timer   = None

        # Pitch/roll history for the 60-second graph
        # Each entry: (monotonic_time, pitch_deg, roll_deg)
        self._angle_history : collections.deque = collections.deque()

        self._silo_ref = None   # set via set_silo(); used for travel_time

        self._try_init()

    # ── Init ──────────────────────────────────────────────────────────────────

    def _try_init(self) -> None:
        if not (_PIC2_OK and _CV2_OK):
            return
        try:
            info = Picamera2.global_camera_info()
            if not info:
                logger.info("No Pi camera detected — video features disabled")
                return
            cam = Picamera2()
            config = cam.create_video_configuration(
                main={"size": (1280, 720)},
                lores={"size": (CAM_W, CAM_H), "format": "YUV420"},
                encode="lores",
                controls={"FrameRate": 30},
            )
            cam.configure(config)
            cam.start()
            self._cam = cam
            self._detected = True
            model = info[0].get("Model", "unknown")
            logger.info("Pi camera started: %s", model)
            threading.Thread(target=self._capture_loop, daemon=True,
                             name="cam-capture").start()
        except Exception as exc:
            logger.warning("Camera init failed: %s", exc)
            if self._cam:
                try:
                    self._cam.close()
                except Exception:
                    pass
            self._cam = None

    # ── MJPEG capture loop ────────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        """Background thread: capture lores frames, build composite, encode JPEG."""
        while self._cam is not None:
            try:
                arr = self._cam.capture_array("lores")
                bgr = cv2.cvtColor(arr, cv2.COLOR_YUV420p2BGR)
                composite = self._build_composite(bgr)

                ok, jpeg = cv2.imencode(
                    ".jpg", composite, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                if ok:
                    with self._lock:
                        self._latest_frame = jpeg.tobytes()

                # Write raw BGR frame to recording FFmpeg process
                with self._lock:
                    proc = self._rec_proc
                if proc is not None and proc.poll() is None:
                    try:
                        proc.stdin.write(composite.tobytes())
                    except BrokenPipeError:
                        logger.warning("Recording FFmpeg pipe closed unexpectedly")
                        with self._lock:
                            self._rec_proc = None
                            self._recording = False
                            self._current_file = None
                        self._notify(False)

            except Exception:
                pass
            time.sleep(1.0 / STREAM_FPS)

    # ── Composite frame builder ────────────────────────────────────────────────

    def _build_composite(self, camera_bgr) -> np.ndarray:
        """Stack camera image above the telemetry bar."""
        bar = self._build_bar()
        return np.vstack([camera_bgr, bar])

    def _build_bar(self) -> np.ndarray:
        """Build the BAR_H × FRAME_W black telemetry strip."""
        bar = np.zeros((BAR_H, FRAME_W, 3), dtype=np.uint8)

        # Update angle history from current telemetry
        t    = self._telem_ref
        tframe = None
        if t is not None:
            try:
                tframe = t.frame
                now = time.monotonic()
                with self._lock:
                    self._angle_history.append((now, tframe.pitch_deg, tframe.roll_deg))
                    # Trim entries older than HISTORY_S
                    cutoff = now - HISTORY_S
                    while self._angle_history and self._angle_history[0][0] < cutoff:
                        self._angle_history.popleft()
            except Exception:
                tframe = None

        self._draw_text_panel(bar, tframe)
        self._draw_graph(bar)

        return bar

    # ── Text panel ────────────────────────────────────────────────────────────

    def _draw_text_panel(self, bar: np.ndarray, f) -> None:
        FIX = {0: "NO FIX", 1: "NO FIX", 2: "2D FIX", 3: "3D FIX",
               4: "DGPS",   5: "RTK FLT", 6: "RTK FIX"}

        now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._lock:
            recording = self._recording
        rec_mark = "[REC]" if recording else "[LIVE]"

        lines = [f"{rec_mark}  {now_str}"]

        if f is not None:
            lines += [
                f"SPD  {f.groundspeed * 1.94384:5.1f} kt    "
                f"ALT  {f.alt_m:6.1f} m",
                f"PCH  {f.pitch_deg:+5.1f}deg    "
                f"ROL  {f.roll_deg:+5.1f}deg",
                f"COG  {f.heading_deg:5.1f}deg    "
                f"GPS  {FIX.get(f.gps_fix, '?')} / {f.satellites} sat",
                f"LAT  {f.lat:10.5f}    "
                f"LON  {f.lon:11.5f}",
            ]
        else:
            lines += ["SPD  ---    ALT  ---",
                      "PCH  ---    ROL  ---",
                      "COG  ---    GPS  ---",
                      "LAT  ---    LON  ---"]

        with self._lock:
            msgs = list(self._messages[-3:])
        for ts_s, text in msgs:
            lines.append(f"[{ts_s}] {text[:50]}")

        font  = cv2.FONT_HERSHEY_DUPLEX
        scale = 0.38
        thick = 1
        lh    = 26
        x0, y0 = 6, 18

        for i, line in enumerate(lines):
            y = y0 + i * lh
            if y > BAR_H - 6:
                break
            cv2.putText(bar, line, (x0 + 1, y + 1), font, scale,
                        (0, 0, 0), thick + 1, cv2.LINE_AA)
            cv2.putText(bar, line, (x0, y), font, scale,
                        (200, 230, 255), thick, cv2.LINE_AA)

        # Vertical divider between text and graph
        cv2.line(bar, (GRAPH_X - 4, 4), (GRAPH_X - 4, BAR_H - 4),
                 (60, 60, 60), 1)

    # ── Pitch/roll graph ──────────────────────────────────────────────────────

    def _draw_graph(self, bar: np.ndarray) -> None:
        """Draw a 60-second pitch/roll strip chart in the right portion of bar."""
        gx = GRAPH_X       # left edge of graph area
        gw = GRAPH_W       # 300 px wide
        gh = BAR_H         # full bar height

        # Graph margins
        mx, my = 28, 14    # left/top margin inside graph area
        pw = gw - mx - 6   # plot width
        ph = gh - my - 16  # plot height

        # Y-axis: ±45 degrees
        y_min, y_max = -45.0, 45.0
        y_range = y_max - y_min

        def to_y(deg):
            frac = (deg - y_min) / y_range
            return int(my + ph - frac * ph)

        def to_x(age_s):
            """age_s = seconds ago (0 = newest, HISTORY_S = oldest visible)"""
            frac = 1.0 - age_s / HISTORY_S
            return int(gx + mx + frac * pw)

        # Grid lines
        for deg in (-30, -20, -10, 0, 10, 20, 30):
            gy = to_y(float(deg))
            col = (80, 80, 80) if deg != 0 else (120, 120, 120)
            cv2.line(bar, (gx + mx, gy), (gx + mx + pw, gy), col, 1)
            lbl = f"{deg:+d}"
            cv2.putText(bar, lbl, (gx + 2, gy + 4),
                        cv2.FONT_HERSHEY_PLAIN, 0.6, (100, 100, 100), 1,
                        cv2.LINE_AA)

        # Time grid lines every 10 s
        now = time.monotonic()
        for s in range(10, HISTORY_S + 1, 10):
            tx = to_x(float(s))
            cv2.line(bar, (tx, my), (tx, my + ph), (50, 50, 50), 1)

        # Axis border
        cv2.rectangle(bar, (gx + mx, my), (gx + mx + pw, my + ph),
                       (80, 80, 80), 1)

        with self._lock:
            history = list(self._angle_history)

        # Plot pitch (cyan) and roll (orange-blue)
        if len(history) >= 2:
            pitch_pts = []
            roll_pts  = []
            for ts, pitch, roll in history:
                age = now - ts
                if age > HISTORY_S:
                    continue
                x = to_x(age)
                pitch_pts.append((x, to_y(max(y_min, min(y_max, pitch)))))
                roll_pts.append( (x, to_y(max(y_min, min(y_max, roll)))))

            if len(pitch_pts) >= 2:
                for i in range(len(pitch_pts) - 1):
                    cv2.line(bar, pitch_pts[i], pitch_pts[i + 1], (0, 220, 220), 1)
            if len(roll_pts) >= 2:
                for i in range(len(roll_pts) - 1):
                    cv2.line(bar, roll_pts[i],  roll_pts[i + 1],  (0, 180, 255), 1)

        # Legend
        lx = gx + mx + pw - 90
        ly = my + 10
        cv2.line(bar, (lx, ly), (lx + 14, ly), (0, 220, 220), 1)
        cv2.putText(bar, "PCH", (lx + 16, ly + 4),
                    cv2.FONT_HERSHEY_PLAIN, 0.6, (0, 220, 220), 1, cv2.LINE_AA)
        cv2.line(bar, (lx + 48, ly), (lx + 62, ly), (0, 180, 255), 1)
        cv2.putText(bar, "ROL", (lx + 64, ly + 4),
                    cv2.FONT_HERSHEY_PLAIN, 0.6, (0, 180, 255), 1, cv2.LINE_AA)

        # Title
        cv2.putText(bar, "60s", (gx + mx, my - 2),
                    cv2.FONT_HERSHEY_PLAIN, 0.65, (120, 120, 120), 1, cv2.LINE_AA)

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_telemetry(self, telem) -> None:
        self._telem_ref = telem

    def set_silo(self, silo) -> None:
        self._silo_ref = silo

    def on_silo_event(self, state: str, source: str) -> None:
        """Called by silo event listener — auto-posts events and controls recording."""
        EVENT_LABELS = {
            "opening": "SILO OPENING",
            "open":    "SILO OPEN",
            "closing": "SILO CLOSING",
            "closed":  "SILO CLOSED",
        }
        label  = EVENT_LABELS.get(state, f"SILO {state.upper()}")
        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        self.post_message(f"{label}  [{ts_str}]  via {source}")

        if state == "opening":
            self.start_recording()
        elif state == "closing":
            # Start stop timer now: 2 × travel_time so the full closing motion
            # is captured.  Cancel any previously scheduled stop first.
            if self._stop_timer is not None:
                self._stop_timer.cancel()
            travel = 2.0
            if self._silo_ref is not None:
                try:
                    travel = self._silo_ref.travel_time
                except Exception:
                    pass
            delay = travel * 2.0
            self._stop_timer = threading.Timer(delay, self.stop_recording)
            self._stop_timer.start()
            logger.debug("Recording stop scheduled in %.1f s (2x travel_time)", delay)

    def start_recording(self) -> dict:
        if not self._detected or self._cam is None:
            return {"ok": False, "reason": "No camera detected"}
        with self._lock:
            if self._recording:
                return {"ok": False, "reason": "Already recording"}
        self._check_disk()
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = VIDEO_DIR / f"PBvideo_{ts}.mp4"
        try:
            cmd = [
                "ffmpeg", "-loglevel", "warning", "-y",
                "-f", "rawvideo",
                "-pixel_format", "bgr24",
                "-video_size", f"{FRAME_W}x{FRAME_H}",
                "-framerate", str(STREAM_FPS),
                "-i", "-",
                "-c:v", "libx264",
                "-crf", str(RECORD_CRF),
                "-preset", "ultrafast",
                str(filepath),
            ]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            with self._lock:
                self._rec_proc     = proc
                self._recording    = True
                self._current_file = filepath
            self._notify(True)
            logger.info("Camera recording started: %s", filepath.name)
            return {"ok": True, "file": filepath.name}
        except Exception as exc:
            logger.error("Failed to start camera recording: %s", exc)
            return {"ok": False, "reason": str(exc)}

    def stop_recording(self) -> dict:
        with self._lock:
            if not self._recording:
                return {"ok": False, "reason": "Not recording"}
            proc  = self._rec_proc
            fname = self._current_file
        try:
            if proc is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
        with self._lock:
            self._rec_proc     = None
            self._recording    = False
            self._current_file = None
        self._notify(False)
        logger.info("Camera recording stopped: %s", fname.name if fname else "?")
        return {"ok": True}

    def snapshot(self) -> bytes | None:
        """Return a JPEG snapshot (latest composite frame)."""
        if not self._detected:
            return None
        with self._lock:
            return self._latest_frame

    def post_message(self, text: str) -> dict:
        """Add a custom status message to the telemetry bar."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with self._lock:
            self._messages.append((ts, str(text)[:120]))
            self._messages = self._messages[-20:]
        logger.info("Camera message: [%s] %s", ts, text)
        return {"ok": True, "ts": ts}

    def get_messages(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._messages)

    def list_videos(self) -> list[dict]:
        files = []
        for p in sorted(VIDEO_DIR.glob("*.mp4"),
                        key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                st = p.stat()
                files.append({
                    "name":     p.name,
                    "size_b":   st.st_size,
                    "size_str": _human_size(st.st_size),
                    "modified": datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M UTC"),
                })
            except Exception:
                pass
        return files

    def status(self) -> dict:
        with self._lock:
            return {
                "camera_detected": self._detected,
                "recording":       self._recording,
                "current_file":    (
                    self._current_file.name
                    if self._current_file and self._recording
                    else None
                ),
            }

    def stream_generator(self):
        """Generator yielding MJPEG multipart chunks for a Flask streaming response."""
        while True:
            with self._lock:
                frame = self._latest_frame
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + frame +
                    b"\r\n"
                )
            time.sleep(1.0 / STREAM_FPS)

    def add_listener(self, fn) -> None:
        with self._lock:
            self._listeners.append(fn)

    def cleanup(self) -> None:
        if self._stop_timer is not None:
            self._stop_timer.cancel()
        with self._lock:
            proc = self._rec_proc
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.terminate()
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception as exc:
                logger.warning("Camera cleanup: %s", exc)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _check_disk(self) -> None:
        try:
            usage    = shutil.disk_usage(VIDEO_DIR)
            pct_free = usage.free / usage.total * 100
            if pct_free < DISK_FREE_MIN_PCT:
                mp4s = sorted(VIDEO_DIR.glob("*.mp4"),
                              key=lambda p: p.stat().st_mtime)
                if mp4s:
                    mp4s[0].unlink()
                    logger.warning(
                        "Disk %.1f%% free — deleted oldest video: %s",
                        pct_free, mp4s[0].name,
                    )
        except Exception as exc:
            logger.warning("Disk check failed: %s", exc)

    def _notify(self, recording: bool) -> None:
        with self._lock:
            fns = list(self._listeners)
        for fn in fns:
            try:
                fn(recording)
            except Exception:
                logger.exception("Camera listener error")
