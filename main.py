#!/usr/bin/env python3
"""
Patrick Blackett Silo 2 Controller — entry point.

Starts four subsystems concurrently:

  Subsystem              Thread        Notes
  ────────────────────────────────────────────────────────────────────
  tkinter GUI            main          Tk event loop (blocks until close)
  Flask web server       daemon        http://<pi-ip>:5000
  MQTT client            daemon        broker on localhost:1883
  MAVLink reader         daemon        /dev/ttyACM0 → telemetry frames
  Stats tracker          daemon        5 Hz rolling-peak calculator
  ────────────────────────────────────────────────────────────────────

On startup, recent CSV recording files (PB*.csv) from the last 30
minutes are loaded and used to pre-seed the stats tracker and track
history so graphs and map are meaningful immediately after a restart.
"""

import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%H:%M:%S",
)

from core.silo import SiloController
from telemetry.reader import TelemetryReader
from telemetry.recorder import Recorder
from telemetry.stats import StatsTracker
from camera.recorder import CameraRecorder
from web import app as web_app
from mqtt.handler import MqttHandler
from gui.app import SiloGUI


def main() -> None:
    silo     = SiloController()
    telem    = TelemetryReader()
    stats    = StatsTracker(telem)
    recorder = Recorder(telem, silo, stats)
    cam      = CameraRecorder()
    cam.set_telemetry(telem)
    cam.set_silo(silo)
    silo.add_event_listener(cam.on_silo_event)

    # ── Seed stats from any recordings in the last 30 minutes ───────────────
    historical = Recorder.load_recent(max_age_s=1800)
    if historical:
        stats.seed(historical)

    # ── MAVLink reader ──────────────────────────────────────────────────────
    telem.start()

    # ── Web server ──────────────────────────────────────────────────────────
    web_app.init(silo, telem, recorder, stats, cam=cam)
    threading.Thread(
        target=web_app.run,
        kwargs={"host": "0.0.0.0", "port": 5000},
        daemon=True,
        name="web-server",
    ).start()

    # ── MQTT client ─────────────────────────────────────────────────────────
    mqtt = MqttHandler(silo, recorder, cam=cam)
    mqtt.start()

    # ── tkinter GUI (blocks until the window is closed) ─────────────────────
    # Skipped automatically when no display is available (headless / systemd).
    try:
        SiloGUI(silo, telem, recorder, stats).run()
    except Exception as exc:
        # TclError: no display name and no $DISPLAY — run headless
        import _tkinter  # noqa: PLC0415
        if isinstance(exc, _tkinter.TclError):
            logging.getLogger(__name__).info(
                "No display available — running headless (web + MQTT only)"
            )
            # Block forever; daemon threads keep everything alive
            import signal
            signal.pause()
        else:
            raise
    finally:
        mqtt.stop()
        telem.stop()
        cam.cleanup()
        silo.cleanup()


if __name__ == "__main__":
    main()
