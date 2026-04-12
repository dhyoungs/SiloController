"""
SiloController core — thread-safe state machine + GPIO driver.

Hardware
--------
Relay module : Eletechsup DR25E01  (3A 1-ch bistable DPDT relay, 5 V logic)
Actuator     : Linak LA 121P00-1101220 (12 V DC linear actuator,
               built-in end-stop limit switches)

How the bistable DPDT relay works
----------------------------------
The relay has three pins:  V (coil power), G (ground), T (trigger).
T is normally held HIGH.  A LOW pulse on T toggles the relay to the
opposite position and latches it there with no power required:

  First  pulse (LOW) → relay moves to position A → extends  (OPEN)
  Second pulse (LOW) → relay moves to position B → retracts (CLOSE)
  ...and so on, alternating each pulse.

Because the relay is a toggle (not level-controlled), the software must
track which position the relay is currently in.  This state is persisted
to STATE_FILE so it survives reboots and service restarts.

If the physical state gets out of sync with the software state (e.g. after
a power cut with the actuator mid-travel), use the Configuration tab in the
web UI to declare the actual silo position.

Wiring (BCM pin numbers)
------------------------
Pi GPIO 17  →  DR25E01 T   (trigger — pulse LOW briefly to toggle)
Pi 5 V      →  DR25E01 V   (coil power, keep T HIGH when idle)
Pi GND      →  DR25E01 G   (ground)
DR25E01 OUT+/OUT−  →  Actuator motor terminals
12 V PSU           →  DR25E01 motor power input (separate from logic)

If open/close are backwards: swap OUT+/OUT− on the actuator terminals.

TRAVEL_TIME is a safety ceiling — the Linak 121P internal limit switches
stop the motor at both ends automatically.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

PIN_TRIGGER           = 17    # DR25E01 T — pulse LOW to toggle relay
PULSE_MS              = 100   # LOW pulse width in milliseconds
DEFAULT_TRAVEL_TIME   = 2.0   # seconds — default travel time (change via UI)

# Persistent state file — survives reboots and service restarts
STATE_FILE   = Path(__file__).parent.parent / "silo_state.json"

try:
    from gpiozero import OutputDevice
    _GPIO_OK = True
except (ImportError, Exception):
    _GPIO_OK = False
    logger.warning("gpiozero not available — running GPIO in simulation mode")

try:
    from gpiozero.exc import BadPinFactory as _BadPinFactory
except ImportError:
    _BadPinFactory = Exception


def _load_state() -> dict:
    """Load persisted state dict from disk."""
    defaults = {"relay_open": False, "travel_time": DEFAULT_TRAVEL_TIME}
    try:
        data = json.loads(STATE_FILE.read_text())
        defaults.update(data)
        logger.info("Loaded state from %s: %s", STATE_FILE, defaults)
    except FileNotFoundError:
        logger.info("No state file — using defaults")
    except Exception as exc:
        logger.warning("Could not read state file (%s) — using defaults", exc)
    return defaults


def _save_state(relay_open: bool, travel_time: float) -> None:
    """Persist state to disk."""
    try:
        STATE_FILE.write_text(json.dumps(
            {"relay_open": relay_open, "travel_time": round(travel_time, 1)},
            indent=2,
        ))
    except Exception as exc:
        logger.warning("Could not save state: %s", exc)


class SiloController:
    """
    Thread-safe silo lid controller.

    State machine:  closed → opening → open → closing → closed

    Two listener types
    ------------------
    add_listener(fn)        fn(state)              — GUI / web / MQTT
    add_event_listener(fn)  fn(state, source)      — recorder (needs source)
    """

    def __init__(self):
        self._lock            = threading.Lock()
        _s = _load_state()
        self._relay_open: bool  = bool(_s["relay_open"])
        self._travel_time: float = float(_s["travel_time"])
        self._state = "open" if self._relay_open else "closed"
        self._listeners:       list[Callable[[str], None]]       = []
        self._event_listeners: list[Callable[[str, str], None]]  = []

        self._trigger = None
        if _GPIO_OK:
            try:
                # Keep T HIGH when idle (relay only toggles on LOW pulse)
                self._trigger = OutputDevice(PIN_TRIGGER, active_high=True, initial_value=True)
                logger.info("GPIO ready — T=GPIO%d  relay_open=%s", PIN_TRIGGER, self._relay_open)
            except (_BadPinFactory, Exception) as exc:
                logger.warning("GPIO init failed (%s) — running in simulation mode", exc)

    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def relay_open(self) -> bool:
        return self._relay_open

    @property
    def travel_time(self) -> float:
        return self._travel_time

    def set_travel_time(self, seconds: float) -> dict:
        """Update travel time and persist it."""
        seconds = max(0.5, min(120.0, float(seconds)))
        self._travel_time = seconds
        _save_state(self._relay_open, self._travel_time)
        logger.info("Travel time set to %.1f s", seconds)
        return {"ok": True, "travel_time": seconds}

    def open(self, source: str = "unknown") -> dict:
        with self._lock:
            if self._state in ("open", "opening"):
                return {"ok": False, "reason": f"Already {self._state}"}
            listeners, ev_listeners = self._transition("opening")
        self._notify(listeners, "opening")
        self._notify_event(ev_listeners, "opening", source)
        threading.Thread(target=self._do_open, daemon=True, name="actuator-open").start()
        return {"ok": True}

    def close(self, source: str = "unknown") -> dict:
        with self._lock:
            if self._state in ("closed", "closing"):
                return {"ok": False, "reason": f"Already {self._state}"}
            listeners, ev_listeners = self._transition("closing")
        self._notify(listeners, "closing")
        self._notify_event(ev_listeners, "closing", source)
        threading.Thread(target=self._do_close, daemon=True, name="actuator-close").start()
        return {"ok": True}

    def declare_state(self, is_open: bool, source: str = "manual") -> dict:
        """Manually declare the physical silo state (Configuration tab).
        Updates the persisted relay state without moving anything."""
        with self._lock:
            self._relay_open = is_open
            new_state = "open" if is_open else "closed"
            listeners, ev_listeners = self._transition(new_state)
        _save_state(is_open, self._travel_time)
        self._notify(listeners, new_state)
        self._notify_event(ev_listeners, new_state, source)
        logger.info("State declared manually: %s (relay_open=%s)", new_state, is_open)
        return {"ok": True, "state": new_state}

    def status(self) -> dict:
        return {"state": self.state}

    def add_listener(self, fn: Callable[[str], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def add_event_listener(self, fn: Callable[[str, str], None]) -> None:
        with self._lock:
            self._event_listeners.append(fn)

    # ── Stress test ─────────────────────────────────────────────────────────────

    def start_stress_test(self, cycles: int, pause_s: float) -> dict:
        """
        Open/close the silo *cycles* times with *pause_s* seconds between moves.
        Runs in a background thread.  Only one test at a time.
        """
        cycles  = max(1, min(1000, int(cycles)))
        pause_s = max(0.5, min(300.0, float(pause_s)))
        with self._lock:
            if getattr(self, "_stress_running", False):
                return {"ok": False, "reason": "Stress test already running"}
            self._stress_running = True
            self._stress_total   = cycles
            self._stress_done    = 0
            self._stress_pause   = pause_s
            self._stress_step    = "starting"
            self._stress_abort   = False
        logger.info("Stress test started: %d cycles  pause=%.1f s", cycles, pause_s)
        threading.Thread(
            target=self._run_stress, daemon=True, name="stress-test"
        ).start()
        return {"ok": True, "cycles": cycles, "pause_s": pause_s}

    def stop_stress_test(self) -> dict:
        with self._lock:
            if not getattr(self, "_stress_running", False):
                return {"ok": False, "reason": "No stress test running"}
            self._stress_abort = True
        logger.info("Stress test stop requested")
        return {"ok": True}

    def stress_test_status(self) -> dict:
        return {
            "running":  getattr(self, "_stress_running", False),
            "done":     getattr(self, "_stress_done",    0),
            "total":    getattr(self, "_stress_total",   0),
            "pause_s":  getattr(self, "_stress_pause",   0),
            "step":     getattr(self, "_stress_step",    "idle"),
        }

    def _run_stress(self) -> None:
        """Background thread body for stress test."""
        def aborted():
            return getattr(self, "_stress_abort", False)

        def _set_step(s):
            with self._lock:
                self._stress_step = s

        def _wait_for(target_state, timeout=60.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if aborted():
                    return False
                if self._state == target_state:
                    return True
                time.sleep(0.1)
            return False

        try:
            total = self._stress_total
            for n in range(total):
                if aborted():
                    break

                # --- OPEN ---
                _set_step(f"cycle {n + 1}/{total} — opening")
                result = self.open(source="stress-test")
                if not result["ok"]:
                    logger.warning("Stress test open rejected: %s", result)
                if not _wait_for("open"):
                    logger.warning("Stress test: timed out waiting for OPEN on cycle %d", n + 1)
                    if aborted():
                        break

                # pause between open and close
                _set_step(f"cycle {n + 1}/{total} — open pause")
                deadline = time.monotonic() + self._stress_pause
                while time.monotonic() < deadline:
                    if aborted():
                        break
                    time.sleep(0.1)
                if aborted():
                    break

                # --- CLOSE ---
                _set_step(f"cycle {n + 1}/{total} — closing")
                result = self.close(source="stress-test")
                if not result["ok"]:
                    logger.warning("Stress test close rejected: %s", result)
                if not _wait_for("closed"):
                    logger.warning("Stress test: timed out waiting for CLOSED on cycle %d", n + 1)
                    if aborted():
                        break

                with self._lock:
                    self._stress_done = n + 1

                if n < total - 1:
                    # pause between cycles
                    _set_step(f"cycle {n + 1}/{total} — inter-cycle pause")
                    deadline = time.monotonic() + self._stress_pause
                    while time.monotonic() < deadline:
                        if aborted():
                            break
                        time.sleep(0.1)
                if aborted():
                    break
        except Exception:
            logger.exception("Stress test error")
        finally:
            with self._lock:
                self._stress_running = False
                self._stress_step    = "idle"
            logger.info(
                "Stress test finished: %d / %d cycles completed",
                getattr(self, "_stress_done", 0),
                getattr(self, "_stress_total", 0),
            )

    def cleanup(self) -> None:
        if _GPIO_OK and self._trigger:
            self._trigger.on()   # return T to HIGH (idle) before closing
            self._trigger.close()

    # ── Internal ────────────────────────────────────────────────────────────

    def _transition(self, new_state: str):
        self._state = new_state
        return list(self._listeners), list(self._event_listeners)

    def _notify(self, listeners: list, state: str) -> None:
        for fn in listeners:
            try:
                fn(state)
            except Exception:
                logger.exception("State listener error")

    def _notify_event(self, listeners: list, state: str, source: str) -> None:
        for fn in listeners:
            try:
                fn(state, source)
            except Exception:
                logger.exception("Event listener error")

    def _toggle_relay(self) -> None:
        """Pulse T LOW for PULSE_MS to toggle relay, then restore HIGH."""
        if _GPIO_OK and self._trigger:
            self._trigger.off()
            time.sleep(PULSE_MS / 1000.0)
            self._trigger.on()
        self._relay_open = not self._relay_open
        _save_state(self._relay_open, self._travel_time)
        logger.info("Relay toggled → relay_open=%s", self._relay_open)

    def _ensure_relay(self, want_open: bool) -> None:
        """Toggle relay only if it isn't already in the desired position."""
        if self._relay_open != want_open:
            self._toggle_relay()
        else:
            logger.info("Relay already relay_open=%s — no toggle needed", want_open)

    def _do_open(self) -> None:
        logger.info("Actuator extending — opening lid")
        self._ensure_relay(want_open=True)
        time.sleep(self._travel_time)
        with self._lock:
            listeners, ev_listeners = self._transition("open")
        self._notify(listeners, "open")
        self._notify_event(ev_listeners, "open", "actuator")
        logger.info("Lid open")

    def _do_close(self) -> None:
        logger.info("Actuator retracting — closing lid")
        self._ensure_relay(want_open=False)
        time.sleep(self._travel_time)
        with self._lock:
            listeners, ev_listeners = self._transition("closed")
        self._notify(listeners, "closed")
        self._notify_event(ev_listeners, "closed", "actuator")
        logger.info("Lid closed")
