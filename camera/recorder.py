"""
Pi Camera recorder — auto-detection, H264 MP4 recording, MJPEG stream.

Design
------
* Detects a Pi camera on startup via Picamera2.global_camera_info().
* If no camera is found (or picamera2/cv2 unavailable) the class works in
  stub mode — all API calls return gracefully so the rest of the app is
  unaffected.
* Recording starts when the silo begins OPENING and stops 2 s after CLOSED.
* A text overlay (UTC time + custom messages) is burned into the recorded
  video only (pre_callback on the main stream).  The MJPEG live stream is
  served clean — telemetry is displayed in the HTML panel around the <img>.
* Rolling buffer: if disk free < DISK_FREE_MIN_PCT the oldest .mp4 is
  deleted before each new recording starts.
* Custom messages are accepted via post_message(); the last three appear in
  the burned-in video overlay.
"""

import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_DIR         = Path(__file__).parent.parent / "recordings" / "video"
DISK_FREE_MIN_PCT = 15.0
STREAM_FPS        = 20     # MJPEG capture rate
RECORD_BITRATE    = 4_000_000   # 4 Mbps H264

# ── Optional deps ─────────────────────────────────────────────────────────────
try:
    from picamera2 import Picamera2, MappedArray
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FfmpegOutput
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
        self._encoder      = None
        self._telem_ref    = None
        self._messages     : list[tuple[str, str]] = []   # (ts_str, text)
        self._latest_frame : bytes | None = None
        self._listeners    : list = []
        self._stop_timer   = None

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
                lores={"size": (640, 360), "format": "YUV420"},
                encode="main",
                controls={"FrameRate": 30},
            )
            cam.configure(config)
            cam.pre_callback = self._draw_overlay
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
        """Background thread: capture lores frames, draw telem overlay, encode as JPEG."""
        while self._cam is not None:
            try:
                arr = self._cam.capture_array("lores")
                bgr = cv2.cvtColor(arr, cv2.COLOR_YUV420p2BGR)
                self._draw_overlay_bgr(bgr)
                ok, jpeg = cv2.imencode(
                    ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                if ok:
                    with self._lock:
                        self._latest_frame = jpeg.tobytes()
            except Exception:
                pass
            time.sleep(1.0 / STREAM_FPS)

    # ── Overlay helpers ────────────────────────────────────────────────────────

    def _build_overlay_lines(self) -> list[str]:
        """Build the list of text lines for the telemetry overlay."""
        FIX = {0: "NO FIX", 1: "NO FIX", 2: "2D FIX", 3: "3D FIX",
               4: "DGPS",   5: "RTK FLT", 6: "RTK FIX"}
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        rec_mark = "\u25cf REC" if self._recording else "\u25cb LIVE"
        lines = [f"{rec_mark}  {now_str}"]

        t = self._telem_ref
        if t is not None:
            try:
                f = t.frame
                lines += [
                    f"SPD  {f.groundspeed * 1.94384:5.1f} kt    "
                    f"ALT  {f.alt_m:6.1f} m",
                    f"PCH  {f.pitch_deg:+5.1f}\u00b0    "
                    f"ROL  {f.roll_deg:+5.1f}\u00b0",
                    f"COG  {f.heading_deg:5.1f}\u00b0    "
                    f"GPS  {FIX.get(f.gps_fix, '?')} / {f.satellites} sat",
                ]
            except Exception:
                pass

        with self._lock:
            msgs = list(self._messages[-3:])
        for ts_s, text in msgs:
            lines.append(f"MSG  [{ts_s}]  {text[:60]}")

        return lines

    def _draw_overlay_bgr(self, bgr) -> None:
        """Draw telem overlay directly onto a BGR numpy array (for MJPEG stream)."""
        try:
            lines = self._build_overlay_lines()
            font  = cv2.FONT_HERSHEY_DUPLEX
            scale = 0.40
            thick = 1
            lh    = 16
            x0, y0 = 8, 16
            for i, line in enumerate(lines):
                y = y0 + i * lh
                cv2.putText(bgr, line, (x0 + 1, y + 1),
                            font, scale, (0, 0, 0), thick + 1, cv2.LINE_AA)
                cv2.putText(bgr, line, (x0, y),
                            font, scale, (200, 230, 255), thick, cv2.LINE_AA)
        except Exception as exc:
            logger.debug("BGR overlay error: %s", exc)

    def _draw_overlay(self, request) -> None:
        """picamera2 pre_callback — burns overlay into the main stream (recorded video)."""
        try:
            with MappedArray(request, "main") as m:
                img     = m.array
                h, w    = img.shape[:2]
                y_plane = img[:h, :w]  # Y (luma) channel of YUV420

                lines = self._build_overlay_lines()
                font  = cv2.FONT_HERSHEY_DUPLEX
                scale = 0.45
                thick = 1
                lh    = 18
                x0, y0 = 8, 18

                for i, line in enumerate(lines):
                    y = y0 + i * lh
                    cv2.putText(y_plane, line, (x0 + 1, y + 1),
                                font, scale, 0, thick + 1, cv2.LINE_AA)
                    cv2.putText(y_plane, line, (x0, y),
                                font, scale, 230, thick, cv2.LINE_AA)
        except Exception as exc:
            logger.debug("Overlay draw error: %s", exc)

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_telemetry(self, telem) -> None:
        self._telem_ref = telem

    def on_silo_event(self, state: str, source: str) -> None:
        """Called by silo event listener — auto-posts timestamped events and controls recording."""
        EVENT_LABELS = {
            "opening": "SILO OPENING",
            "open":    "SILO OPEN",
            "closing": "SILO CLOSING",
            "closed":  "SILO CLOSED",
        }
        label = EVENT_LABELS.get(state, f"SILO {state.upper()}")
        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        self.post_message(f"{label}  [{ts_str}]  via {source}")

        if state == "opening":
            self.start_recording()
        elif state == "closed":
            if self._stop_timer is not None:
                self._stop_timer.cancel()
            self._stop_timer = threading.Timer(2.0, self.stop_recording)
            self._stop_timer.start()

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
            encoder = H264Encoder(bitrate=RECORD_BITRATE)
            output  = FfmpegOutput(str(filepath))
            self._cam.start_encoder(encoder, output)
            with self._lock:
                self._recording    = True
                self._current_file = filepath
                self._encoder      = encoder
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
            fname = self._current_file
        try:
            self._cam.stop_encoder()
        except Exception as exc:
            logger.warning("Stop encoder error: %s", exc)
        with self._lock:
            self._recording    = False
            self._encoder      = None
            self._current_file = None
        self._notify(False)
        logger.info("Camera recording stopped: %s", fname.name if fname else "?")
        return {"ok": True}

    def snapshot(self) -> bytes | None:
        """Return a JPEG snapshot (latest frame from MJPEG buffer, or direct capture)."""
        if not self._detected:
            return None
        with self._lock:
            frame = self._latest_frame
        if frame:
            return frame
        try:
            arr = self._cam.capture_array("lores")
            bgr = cv2.cvtColor(arr, cv2.COLOR_YUV420p2BGR)
            ok, jpeg = cv2.imencode(".jpg", bgr,
                                    [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            return jpeg.tobytes() if ok else None
        except Exception:
            return None

    def post_message(self, text: str) -> dict:
        """Add a custom status message to the video overlay log."""
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
        if self._cam is not None:
            try:
                if self._recording:
                    self._cam.stop_encoder()
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
