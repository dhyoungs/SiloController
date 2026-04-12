"""
MAVLink telemetry reader for Matek H743 + mLRS.

Connects over serial (/dev/ttyACM0 by default), requests a 10 Hz data
stream, and keeps the most recent TelemetryFrame available via .frame.

MAVLink messages used
---------------------
GLOBAL_POSITION_INT   → lat, lon, alt, heading, groundspeed
ATTITUDE              → pitch, roll, yaw
GPS_RAW_INT           → fix_type, satellites_visible, hdop, vdop, h_acc, v_acc
GPS_STATUS            → per-satellite: PRN, elevation, azimuth, SNR, used
AUTOPILOT_VERSION     → firmware version, board UID, capabilities
SYS_STATUS            → sensor health bitmask, battery
HEARTBEAT             → autopilot type, vehicle type, base_mode
STATUSTEXT            → rolling log of messages from FC
EKF_STATUS_REPORT     → EKF variance flags

Serial defaults
---------------
Port : /dev/ttyACM0  (Matek H743 MAVLink port on USB)
Baud : 57600         (mLRS default)

If pymavlink is not installed the reader automatically falls back to a
sine-wave simulation so the rest of the application can still run.
"""

import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

SERIAL_PORT = None    # None = auto-detect; or set a specific path e.g. "/dev/ttyACM0"
BAUD_RATE   = 57600

# ── Calibration offsets ───────────────────────────────────────────────────────
# Stored in calibration.json at the project root.
# pitch_offset and roll_offset (degrees) are subtracted from raw MAVLink values
# so the display reads zero when the vessel is at its neutral floating attitude.

_CAL_FILE = Path(__file__).parent.parent / "calibration.json"
_cal_lock = threading.Lock()
_cal: dict = {"pitch_offset": 0.0, "roll_offset": 0.0}


def _load_calibration() -> None:
    global _cal
    try:
        data = json.loads(_CAL_FILE.read_text())
        with _cal_lock:
            _cal["pitch_offset"] = float(data.get("pitch_offset", 0.0))
            _cal["roll_offset"]  = float(data.get("roll_offset",  0.0))
        logger.info("Calibration loaded: pitch_offset=%.2f  roll_offset=%.2f",
                    _cal["pitch_offset"], _cal["roll_offset"])
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Could not load calibration: %s", exc)


def _save_calibration() -> None:
    try:
        with _cal_lock:
            data = dict(_cal)
        _CAL_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.warning("Could not save calibration: %s", exc)


def get_calibration() -> dict:
    with _cal_lock:
        return dict(_cal)


def set_calibration(pitch_offset: float, roll_offset: float) -> dict:
    """Set and persist calibration offsets."""
    pitch_offset = round(float(pitch_offset), 3)
    roll_offset  = round(float(roll_offset),  3)
    with _cal_lock:
        _cal["pitch_offset"] = pitch_offset
        _cal["roll_offset"]  = roll_offset
    _save_calibration()
    logger.info("Calibration set: pitch_offset=%.3f  roll_offset=%.3f",
                pitch_offset, roll_offset)
    return {"pitch_offset": pitch_offset, "roll_offset": roll_offset}


def capture_level_calibration(raw_pitch: float, raw_roll: float) -> dict:
    """Capture current attitude as the 'level' reference (offsets = current values)."""
    return set_calibration(raw_pitch, raw_roll)


_load_calibration()

# Glob patterns to search for MAVLink devices (in priority order)
_PORT_GLOBS = ["/dev/ttyACM*", "/dev/ttyUSB*"]

try:
    from pymavlink import mavutil as _mavutil
    _MAV_OK = True
except ImportError:
    _MAV_OK = False
    logger.warning("pymavlink not installed — running telemetry in simulation mode")


@dataclass
class TelemetryFrame:
    lat:           float = 0.0   # decimal degrees
    lon:           float = 0.0
    alt_m:         float = 0.0   # metres MSL
    heading_deg:   float = 0.0   # 0–360 (course over ground from GPS)
    groundspeed:   float = 0.0   # m/s
    pitch_deg:     float = 0.0   # degrees, +nose-up
    roll_deg:      float = 0.0   # degrees, +starboard heel
    yaw_deg:       float = 0.0   # degrees 0–360, from ATTITUDE (magnetic)
    gps_fix:       int   = 0     # 0=none, 2=2D, 3=3D, 6=RTK
    satellites:    int   = 0
    hdop:          float = 99.9  # horizontal dilution of precision
    vdop:          float = 99.9  # vertical dilution of precision
    h_acc:         float = 0.0   # horizontal accuracy estimate (m)
    v_acc:         float = 0.0   # vertical accuracy estimate (m)
    valid:         bool  = False  # True once first position received


class TelemetryReader:
    def __init__(self, port: str = SERIAL_PORT, baud: int = BAUD_RATE):
        self._port    = port
        self._baud    = baud
        self._lock    = threading.Lock()
        self._frame   = TelemetryFrame()
        self._running = False
        self._conn    = None   # active mavlink connection (for redetect)

        # ── Diagnostics store (updated from MAVLink, read via .diagnostics) ──
        self._diag: dict[str, Any] = {
            # Connection
            "connected":       False,
            "sysid":           None,
            "compid":          None,
            "port":            port,
            "baud":            baud,
            # Autopilot identity (AUTOPILOT_VERSION)
            "fw_version":      None,   # "x.y.z"
            "fw_type":         None,   # "ArduPilot" etc
            "board_version":   None,
            "hw_uid":          None,   # hex string
            "capabilities":    None,   # hex bitmask
            "os_sw_version":   None,
            "flight_sw_version": None,
            # Vehicle type (HEARTBEAT)
            "autopilot_type":  None,
            "vehicle_type":    None,
            "base_mode":       None,
            "custom_mode":     None,
            "system_status":   None,
            # GPS module identity (captured from STATUSTEXT + params on connect)
            "gps_module": {
                "detected":    False,
                "type_str":    None,   # e.g. "u-blox M10"
                "hw_version":  None,   # e.g. "00190000"
                "sw_version":  None,   # e.g. "EXT CORE 1.00 (3d457f)"
                "proto":       None,   # protocol version string
                "serial":      None,   # unique ID if available
                "gnss_mode":   None,   # GPS1_GNSS_MODE value
                "type_id":     None,   # GPS1_TYPE numeric
                "rate_ms":     None,   # GPS1_RATE_MS
                "com_port":    None,   # GPS1_COM_PORT
                "auto_config": None,   # GPS_AUTO_CONFIG
                "save_cfg":    None,   # GPS_SAVE_CFG
                "statustext":  [],     # raw GPS STATUSTEXT lines captured
            },
            # GPS (GPS_RAW_INT extended)
            "gps_type":        None,
            "gps_noise":       None,   # jamming indicator
            "gps_jamming":     None,
            # Per-satellite info (GPS_STATUS)
            "satellites_info": [],     # list of {prn, elevation, azimuth, snr, used}
            # Sensor health (SYS_STATUS)
            "sensors_present": None,   # hex
            "sensors_enabled": None,
            "sensors_healthy": None,
            "sensors_unhealthy": [],   # list of sensor names
            "voltage_mv":      None,
            "current_ca":      None,   # centi-amps
            "battery_pct":     None,
            # EKF health (EKF_STATUS_REPORT)
            "ekf_flags":       None,
            "ekf_vel_var":     None,
            "ekf_pos_h_var":   None,
            "ekf_pos_v_var":   None,
            "ekf_compass_var": None,
            "ekf_terrain_alt_var": None,
            # Rolling status log
            "statustext":      deque(maxlen=30),  # list of {ts, sev, text}
            # mLRS / radio link (RADIO_STATUS)
            "radio_rssi":      None,   # 0-254 local RSSI (255=invalid)
            "radio_remrssi":   None,   # remote RSSI
            "radio_txbuf":     None,   # tx buffer utilisation %
            "radio_noise":     None,
            "radio_remnoise":  None,
            "radio_rxerrors":  None,   # cumulative packet errors
            "radio_fixed":     None,   # packets repaired by FEC
            "radio_ts":        None,   # timestamp of last RADIO_STATUS
            # RC / mLRS channel link quality
            "rc_rssi":         None,
            "rc_chan_count":   None,
            "mlrs_lq_pct":     None,   # link quality 0-100%
            "mlrs_rssi_dbm":   None,   # RSSI in dBm (estimated)
        }

    # ── Public ───────────────────────────────────────────────────────────────

    @property
    def frame(self) -> TelemetryFrame:
        """Thread-safe snapshot of the latest telemetry."""
        with self._lock:
            f = self._frame
            return TelemetryFrame(
                lat=f.lat,           lon=f.lon,
                alt_m=f.alt_m,       heading_deg=f.heading_deg,
                groundspeed=f.groundspeed,
                pitch_deg=f.pitch_deg, roll_deg=f.roll_deg,
                yaw_deg=f.yaw_deg,
                gps_fix=f.gps_fix,   satellites=f.satellites,
                hdop=f.hdop,         vdop=f.vdop,
                h_acc=f.h_acc,       v_acc=f.v_acc,
                valid=f.valid,
            )

    @property
    def diagnostics(self) -> dict:
        """Thread-safe snapshot of all diagnostic data."""
        with self._lock:
            d = dict(self._diag)
            # Convert deque to list for JSON serialisation
            d["statustext"] = list(self._diag["statustext"])
            d["satellites_info"] = list(self._diag["satellites_info"])
            return d

    def start(self) -> None:
        self._running = True
        threading.Thread(
            target=self._run_loop, daemon=True, name="mavlink-reader"
        ).start()
        logger.info("TelemetryReader started (port=%s baud=%d)", self._port, self._baud)

    def stop(self) -> None:
        self._running = False


    # ── Internal ─────────────────────────────────────────────────────────────

    def _find_port(self) -> "str | None":
        """Scan _PORT_GLOBS for a MAVLink heartbeat. Returns port path or None."""
        import glob as _glob
        candidates: list[str] = []
        for pattern in _PORT_GLOBS:
            candidates.extend(sorted(_glob.glob(pattern)))
        # Prefer the last-known port if it still exists
        if self._port and self._port in candidates:
            candidates = [self._port] + [c for c in candidates if c != self._port]
        for port in candidates:
            logger.info("Probing %s for MAVLink heartbeat …", port)
            try:
                conn = _mavutil.mavlink_connection(port, baud=self._baud)
                hb = conn.wait_heartbeat(timeout=4)
                conn.close()
                if hb is not None:
                    logger.info("MAVLink device found on %s", port)
                    return port
            except Exception as exc:
                logger.debug("  %s: %s", port, exc)
        return None

    def _run_loop(self) -> None:
        while self._running:
            try:
                if _MAV_OK:
                    self._read_mavlink()
                else:
                    self._simulate()
            except Exception as exc:
                logger.error("Telemetry error: %s — retrying in 5 s", exc)
                with self._lock:
                    self._diag["connected"] = False
                time.sleep(5)

    def _read_mavlink(self) -> None:
        # Resolve port — auto-detect if not fixed
        port = self._port
        if port is None:
            port = self._find_port()
            if port is None:
                logger.warning("No MAVLink device found — retrying in 10 s")
                time.sleep(10)
                return
            with self._lock:
                self._diag["port"] = port

        logger.info("MAVLink connecting on %s @ %d baud", port, self._baud)
        conn = _mavutil.mavlink_connection(port, baud=self._baud)

        hb = conn.wait_heartbeat(timeout=15)
        if hb is None:
            conn.close()
            # Port didn't respond — clear cached choice so next loop re-probes
            with self._lock:
                self._diag["connected"] = False
                self._diag["port"] = None
            raise RuntimeError("No MAVLink heartbeat within 15 s")

        with self._lock:
            self._diag["connected"] = True
            self._diag["sysid"]     = conn.target_system
            self._diag["compid"]    = conn.target_component
        self._conn = conn

        logger.info("Heartbeat — sysid=%d compid=%d",
                    conn.target_system, conn.target_component)

        # Request all data streams at 10 Hz
        conn.mav.request_data_stream_send(
            conn.target_system, conn.target_component,
            _mavutil.mavlink.MAV_DATA_STREAM_ALL, 10, 1,
        )

        # Request autopilot capabilities
        conn.mav.command_long_send(
            conn.target_system, conn.target_component,
            _mavutil.mavlink.MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES,
            0, 1, 0, 0, 0, 0, 0, 0,
        )

        # Request GPS-related parameters (responses handled in _ingest)
        for p in self._GPS_PARAMS:
            conn.mav.param_request_read_send(
                conn.target_system, conn.target_component,
                p.encode(), -1,
            )

        _missed = 0
        while self._running:
            msg = conn.recv_match(blocking=True, timeout=2.0)
            if msg is None:
                _missed += 1
                if _missed >= 10:   # 20 s silence → treat as disconnected
                    logger.warning("MAVLink silence on %s — disconnecting", port)
                    break
                continue
            _missed = 0
            self._ingest(msg)

        conn.close()
        self._conn = None
        with self._lock:
            self._diag["connected"] = False
            # Clear port so next _run_loop iteration re-probes from scratch
            self._diag["port"] = None

    _GPS_PARAMS = {
        "GPS1_TYPE", "GPS1_GNSS_MODE", "GPS1_RATE_MS",
        "GPS1_COM_PORT", "GPS_AUTO_CONFIG", "GPS_SAVE_CFG",
    }
    _TYPE_NAMES = {
        0:"None/Disabled",1:"AUTO",2:"u-blox",5:"NMEA",9:"u-blox (auto)",
        17:"u-blox MovingBase",18:"u-blox RelPos",
    }
    _GNSS_NAMES = {
        0:"Default (GPS+SBAS)",1:"GPS only",3:"GPS+SBAS",
        7:"GPS+SBAS+Galileo",15:"All constellations",67:"GPS+GLONASS",
    }

    def _ingest(self, msg) -> None:  # noqa: C901
        mt = msg.get_type()
        with self._lock:
            # ── Telemetry frame ─────────────────────────────────────────────
            if mt == "GLOBAL_POSITION_INT":
                self._frame.lat         = msg.lat / 1e7
                self._frame.lon         = msg.lon / 1e7
                self._frame.alt_m       = msg.alt / 1_000.0
                self._frame.heading_deg = msg.hdg / 100.0
                vx = msg.vx / 100.0
                vy = msg.vy / 100.0
                self._frame.groundspeed = math.hypot(vx, vy)
                self._frame.valid       = True

            elif mt == "ATTITUDE":
                raw_pitch = math.degrees(msg.pitch)
                raw_roll  = math.degrees(msg.roll)
                with _cal_lock:
                    self._frame.pitch_deg = raw_pitch - _cal["pitch_offset"]
                    self._frame.roll_deg  = raw_roll  - _cal["roll_offset"]
                self._frame.yaw_deg   = math.degrees(msg.yaw) % 360

            elif mt == "GPS_RAW_INT":
                self._frame.gps_fix    = msg.fix_type
                self._frame.satellites = msg.satellites_visible
                if msg.eph != 65535:
                    self._frame.hdop = msg.eph / 100.0
                if msg.epv != 65535:
                    self._frame.vdop = msg.epv / 100.0
                # h_acc / v_acc added in MAVLink 2
                if hasattr(msg, 'h_acc') and msg.h_acc != 0:
                    self._frame.h_acc = msg.h_acc / 1000.0
                if hasattr(msg, 'v_acc') and msg.v_acc != 0:
                    self._frame.v_acc = msg.v_acc / 1000.0
                # Noise/jamming indicators
                if hasattr(msg, 'noise_per_ms'):
                    self._diag["gps_noise"]   = msg.noise_per_ms
                if hasattr(msg, 'jamming_indicator'):
                    self._diag["gps_jamming"] = msg.jamming_indicator

            # ── mLRS / radio link ───────────────────────────────────────────
            elif mt == "RADIO_STATUS":
                # Sent by mLRS (and SiK/RFD modems) — local and remote link stats
                self._diag["radio_rssi"]      = msg.rssi      # 0-254 (255=invalid)
                self._diag["radio_remrssi"]   = msg.remrssi
                self._diag["radio_txbuf"]     = msg.txbuf     # tx buffer %
                self._diag["radio_noise"]     = msg.noise
                self._diag["radio_remnoise"]  = msg.remnoise
                self._diag["radio_rxerrors"]  = msg.rxerrors  # cumulative errors
                self._diag["radio_fixed"]     = msg.fixed     # packets repaired
                self._diag["radio_ts"]        = (lambda n: n.strftime(f"%H:%M:%S.{n.microsecond // 1000:03d}"))(datetime.now(timezone.utc))

            elif mt == "RC_CHANNELS":
                # mLRS puts link quality in chan16 (0-100 scaled to 0-1000)
                self._diag["rc_rssi"]         = msg.rssi      # 0-254 or 255=invalid
                self._diag["rc_chan_count"]    = msg.chancount
                # mLRS LQ is typically on chan17 or chan18 (index 16/17), 0-100 mapped to 1000-2000
                try:
                    raw_lq = getattr(msg, 'chan17_raw', None)
                    if raw_lq and raw_lq != 65535:
                        # mLRS: 1000=0%, 2000=100%
                        self._diag["mlrs_lq_pct"] = max(0, min(100, (raw_lq - 1000) // 10))
                    raw_rssi = getattr(msg, 'chan18_raw', None)
                    if raw_rssi and raw_rssi != 65535:
                        self._diag["mlrs_rssi_dbm"] = -((2000 - raw_rssi) // 4 + 50)
                except Exception:
                    pass

            elif mt == "RADIO":
                # Older SiK-style RADIO message (MAVLink v1 equivalent of RADIO_STATUS)
                self._diag["radio_rssi"]     = getattr(msg, 'rssi', None)
                self._diag["radio_remrssi"]  = getattr(msg, 'remrssi', None)
                self._diag["radio_rxerrors"] = getattr(msg, 'rxerrors', None)
                self._diag["radio_fixed"]    = getattr(msg, 'fixed', None)

            # ── Diagnostics ─────────────────────────────────────────────────

            elif mt == "GPS_STATUS":
                sats = []
                for i in range(msg.satellites_visible):
                    try:
                        sats.append({
                            "prn":       msg.satellite_prn[i],
                            "elevation": msg.satellite_elevation[i],
                            "azimuth":   msg.satellite_azimuth[i],
                            "snr":       msg.satellite_snr[i],
                            "used":      msg.satellite_used[i] != 0,
                        })
                    except IndexError:
                        break
                self._diag["satellites_info"] = sats

            elif mt == "AUTOPILOT_VERSION":
                fv   = msg.flight_sw_version
                fw_s = f"{(fv>>24)&0xFF}.{(fv>>16)&0xFF}.{(fv>>8)&0xFF}"
                self._diag["fw_version"]      = fw_s
                self._diag["flight_sw_version"] = fv
                self._diag["os_sw_version"]   = msg.os_sw_version
                self._diag["board_version"]   = msg.board_version
                self._diag["capabilities"]    = hex(msg.capabilities)
                uid = msg.uid2 if hasattr(msg, 'uid2') and any(msg.uid2) else msg.uid
                try:
                    self._diag["hw_uid"] = bytes(uid).hex().upper()
                except Exception:
                    self._diag["hw_uid"] = str(uid)

            elif mt == "HEARTBEAT":
                ap_names = {
                    0:"Generic",3:"ArduPilot",8:"ArduPilot",
                    12:"PX4",18:"ArduPilot"
                }
                vt_names = {
                    0:"Generic",1:"Fixed-wing",2:"Quadrotor",
                    10:"Ground",11:"Sub",12:"Blimp",
                    19:"VTOL",29:"Boat",
                }
                self._diag["autopilot_type"] = ap_names.get(
                    msg.autopilot, f"AP#{msg.autopilot}")
                self._diag["vehicle_type"]   = vt_names.get(
                    msg.type, f"Type#{msg.type}")
                self._diag["base_mode"]      = msg.base_mode
                self._diag["custom_mode"]    = msg.custom_mode
                self._diag["system_status"]  = msg.system_status

            elif mt == "SYS_STATUS":
                self._diag["sensors_present"] = msg.onboard_control_sensors_present
                self._diag["sensors_enabled"] = msg.onboard_control_sensors_enabled
                self._diag["sensors_healthy"] = msg.onboard_control_sensors_health
                self._diag["voltage_mv"]      = msg.voltage_battery
                self._diag["current_ca"]      = msg.current_battery
                self._diag["battery_pct"]     = msg.battery_remaining
                # Identify unhealthy sensors
                enabled  = msg.onboard_control_sensors_enabled
                healthy  = msg.onboard_control_sensors_health
                unhealthy = _sensor_names(enabled & ~healthy)
                self._diag["sensors_unhealthy"] = unhealthy

            elif mt == "EKF_STATUS_REPORT":
                self._diag["ekf_flags"]           = msg.flags
                self._diag["ekf_vel_var"]          = round(msg.velocity_variance, 3)
                self._diag["ekf_pos_h_var"]        = round(msg.pos_horiz_variance, 3)
                self._diag["ekf_pos_v_var"]        = round(msg.pos_vert_variance, 3)
                self._diag["ekf_compass_var"]      = round(msg.compass_variance, 3)
                if hasattr(msg, 'terrain_alt_variance'):
                    self._diag["ekf_terrain_alt_var"] = round(msg.terrain_alt_variance, 3)

            elif mt == "PARAM_VALUE":
                pid = msg.param_id.strip('\x00')
                if pid in self._GPS_PARAMS:
                    v = int(msg.param_value)
                    gm = self._diag["gps_module"]
                    if pid == "GPS1_TYPE":
                        gm["type_id"]   = v
                        gm["type_str"]  = self._TYPE_NAMES.get(v, f"Type {v}")
                    elif pid == "GPS1_GNSS_MODE":
                        gm["gnss_mode"] = self._GNSS_NAMES.get(v, f"Mode {v}")
                    elif pid == "GPS1_RATE_MS":
                        gm["rate_ms"]   = v
                    elif pid == "GPS1_COM_PORT":
                        gm["com_port"]  = v
                    elif pid == "GPS_AUTO_CONFIG":
                        gm["auto_config"] = v
                    elif pid == "GPS_SAVE_CFG":
                        gm["save_cfg"]  = v

            elif mt == "STATUSTEXT":
                sev_names = {
                    0:"EMERG",1:"ALERT",2:"CRIT",3:"ERROR",
                    4:"WARN", 5:"NOTE",6:"INFO",7:"DEBUG"
                }
                txt = msg.text.rstrip("\x00")
                self._diag["statustext"].append({
                    "ts":   (lambda n: n.strftime(f"%H:%M:%S.{n.microsecond // 1000:03d}"))(datetime.now(timezone.utc)),
                    "sev":  sev_names.get(msg.severity, str(msg.severity)),
                    "text": txt,
                })
                # Capture GPS module version info from ArduPilot startup messages
                # ArduPilot sends e.g. "u-blox 1 HW: 00190000 SW: EXT CORE 1.00 (3d457f)"
                txt_low = txt.lower()
                if any(k in txt_low for k in ("u-blox", "ublox", "gps", "m10", "m8n", "m9n")):
                    gm = self._diag["gps_module"]
                    if txt not in gm["statustext"]:
                        gm["statustext"].append(txt)
                    import re as _re
                    # "u-blox 1 HW: XXXXXXXX SW: ..."
                    hw_m = _re.search(r'HW[:\s]+([0-9A-Fa-f]{6,})', txt)
                    if hw_m:
                        gm["hw_version"] = hw_m.group(1).upper()
                    sw_m = _re.search(r'SW[:\s]+([\w\s\.\(\)]+?)(?:$|\n|PROT|SER)', txt)
                    if sw_m:
                        gm["sw_version"] = sw_m.group(1).strip()
                    proto_m = _re.search(r'PROT[:\s]+([\d\.]+)', txt)
                    if proto_m:
                        gm["proto"] = proto_m.group(1)
                    ser_m = _re.search(r'SER[:\s]+(\d+)', txt)
                    if ser_m:
                        gm["serial"] = ser_m.group(1)
                    # "GPS: u-blox M10 detected" / "u-blox 1 M10"
                    type_m = _re.search(r'(M10|M9N|M8N|M8Q|ZED-F9|NEO-M\w+)', txt, _re.I)
                    if type_m:
                        gm["type_str"]  = type_m.group(1).upper()
                        gm["detected"]  = True

    def _simulate(self) -> None:
        """Smooth sine-wave fake telemetry — used when pymavlink is absent."""
        t = 0.0
        with self._lock:
            self._diag["connected"]  = True
            self._diag["fw_version"] = "SIM"
            self._diag["autopilot_type"] = "Simulation"
            self._diag["vehicle_type"]   = "Boat"
        while self._running:
            with self._lock:
                self._frame.lat         = 51.5000 + 0.0005 * math.sin(t * 0.05)
                self._frame.lon         = -0.1000 + 0.0005 * math.cos(t * 0.05)
                self._frame.alt_m       = 55.0 + 3.0 * math.sin(t * 0.15)
                self._frame.heading_deg = (t * 8.0) % 360.0
                self._frame.groundspeed = 4.0 + 1.5 * math.sin(t * 0.2)
                self._frame.pitch_deg   = 6.0 * math.sin(t * 0.4)
                self._frame.roll_deg    = 15.0 * math.sin(t * 0.3)
                self._frame.yaw_deg     = (t * 8.0) % 360.0
                self._frame.gps_fix     = 3
                self._frame.satellites  = 9
                self._frame.hdop        = 1.2
                self._frame.vdop        = 1.8
                self._frame.valid       = True
            time.sleep(0.1)
            t += 0.1


# ── Sensor name helper ────────────────────────────────────────────────────────

_SENSOR_BITS = [
    (0x00000001, "3D_GYRO"),    (0x00000002, "3D_ACCEL"),
    (0x00000004, "3D_MAG"),     (0x00000008, "ABS_PRESSURE"),
    (0x00000010, "DIFF_PRESSURE"),(0x00000020,"GPS"),
    (0x00000040, "OPTICAL_FLOW"),(0x00000080,"COMPUTER_VISION"),
    (0x00000100, "LASER_ALT"),  (0x00000200, "EXT_GROUND_TRUTH"),
    (0x00000400, "ANG_RATE_CTRL"),(0x00000800,"ATTITUDE_CTRL"),
    (0x00001000, "YAW_POS"),    (0x00002000, "Z_ALT_CTRL"),
    (0x00004000, "XY_POS_CTRL"),(0x00008000, "MOTOR_CTRL"),
    (0x00010000, "RC_RECEIVER"),(0x00020000, "3D_GYRO2"),
    (0x00040000, "3D_ACCEL2"),  (0x00080000, "3D_MAG2"),
    (0x00100000, "GEOFENCE"),   (0x00200000, "AHRS"),
    (0x00400000, "TERRAIN"),    (0x00800000, "REVERSE_MOTOR"),
    (0x01000000, "LOGGING"),    (0x02000000, "BATTERY"),
    (0x04000000, "PROXIMITY"),  (0x08000000, "SATCOM"),
    (0x10000000, "PREARM_CHECK"),(0x20000000,"OBSTACLE_AVOIDANCE"),
]

def _sensor_names(bitmask: int) -> list[str]:
    return [name for bit, name in _SENSOR_BITS if bitmask & bit]
