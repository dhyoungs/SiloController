#!/usr/bin/env python3
"""
GPS cold-start assistance injector — no registration required.

Injects three types of assistance data into the u-blox GPS via MAVLink:
  1. Current UTC time  (from Pi system clock, NTP-synced)
  2. Approximate position (configurable; defaults to Solent/English Channel)
  3. GPS almanac       (downloaded from CelesTrak — free, no account needed)

Usage:
    python tools/gps_assist.py [--port /dev/ttyACM1] [--lat 50.8 --lon -1.1]

Without --lat/--lon it uses the Solent as the approximate position (~50 km
accuracy is fine; the receiver only needs a rough starting point).
"""

import argparse
import datetime
import glob
import logging
import math
import struct
import sys
import time
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Default approximate position (PO18 9AB — Bosham/Chichester, W Sussex) ────
DEFAULT_LAT =  50.830  # degrees N
DEFAULT_LON =  -0.870  # degrees E (negative = West)
DEFAULT_ALT =   5.0    # metres MSL
POS_ACC_M   = 50_000   # position accuracy (50 km — very conservative)

# ── YUMA almanac URL (US Coast Guard Navigation Center — free, no login) ──────
YUMA_URL = "https://www.navcen.uscg.gov/sites/default/files/gps/almanac/current_yuma.alm"

# ── UBX framing ───────────────────────────────────────────────────────────────

def _ubx_checksum(payload: bytes) -> tuple[int, int]:
    ck_a = ck_b = 0
    for b in payload:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def _ubx_frame(cls: int, msg_id: int, payload: bytes) -> bytes:
    header = bytes([0xB5, 0x62, cls, msg_id]) + struct.pack("<H", len(payload))
    body   = header[2:] + payload          # checksum covers class→end of payload
    ck_a, ck_b = _ubx_checksum(body)
    return header + payload + bytes([ck_a, ck_b])


# ── UBX-MGA-INI-TIME_UTC  (class 0x13, id 0x40, subtype 0x10) ────────────────

def build_mga_ini_time(dt: datetime.datetime) -> bytes:
    """Build a UBX-MGA-INI-TIME_UTC message from a UTC datetime."""
    payload = struct.pack(
        "<BBHHBBBBBBIHHi",
        0x10,           # type = UTC time
        0x00,           # version
        0x0000,         # reserved
        dt.year,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        0x00,           # reserved
        0,              # nanoseconds (0 = ignore sub-second)
        2,              # tAccS  — accuracy 2 seconds (generous)
        0x0000,         # reserved
        0,              # tAccNs
    )
    return _ubx_frame(0x13, 0x40, payload)


# ── UBX-MGA-INI-POS_LLH  (class 0x13, id 0x40, subtype 0x01) ────────────────

def build_mga_ini_pos(lat_deg: float, lon_deg: float, alt_m: float,
                      acc_m: float = POS_ACC_M) -> bytes:
    """Build a UBX-MGA-INI-POS_LLH message."""
    payload = struct.pack(
        "<BBHiiiI",
        0x01,                       # type = LLH position
        0x00,                       # version
        0x0000,                     # reserved
        int(lat_deg * 1e7),         # lat  1e-7 deg
        int(lon_deg * 1e7),         # lon  1e-7 deg
        int(alt_m * 100),           # alt  cm
        int(acc_m * 100),           # posAcc cm
    )
    return _ubx_frame(0x13, 0x40, payload)


# ── YUMA almanac parser ───────────────────────────────────────────────────────

def _parse_yuma(text: str) -> list[dict]:
    """Parse a YUMA-format almanac file into a list of satellite dicts."""
    sats = []
    current: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ID:"):
            if current:
                sats.append(current)
            current = {"svid": int(line.split()[-1])}
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower().replace(" ", "_")
            try:
                current[key] = float(val.strip())
            except ValueError:
                current[key] = val.strip()
    if current:
        sats.append(current)
    return sats


# ── UBX-MGA-GPS-ALM  (class 0x13, id 0x00) ───────────────────────────────────
# UBX-MGA-GPS-ALM payload layout (36 bytes, u-blox M10 IDD):
#   B  type=0   B version=0   B svId   B svHealth
#   I  e        (2^-21, unsigned)
#   I  almToa   (2^12 s  → stored = toa_s / 4096, unsigned)
#   h  deltaI   (2^-19 semicircles, delta from ref inclination 0.3π rad)
#   h  omegaDot (2^-38 semicircles/s)
#   I  sqrtA    (2^-11 m^0.5, unsigned)
#   i  omega0   (2^-23 semicircles, signed)
#   i  omega    (2^-23 semicircles, signed)
#   i  m0       (2^-23 semicircles, signed)
#   h  af0      (2^-20 s)
#   h  af1      (2^-38 s/s)

_REF_I = 0.3 * math.pi   # GPS reference orbital inclination (rad)

def build_mga_gps_alm(sat: dict) -> bytes | None:
    """Convert a YUMA satellite dict to a UBX-MGA-GPS-ALM message."""

    def _sc_u(val: float, lsb: float) -> int:
        return max(0, min(0xFFFF_FFFF, int(round(val / lsb))))

    def _sc_i32(val: float, lsb: float) -> int:
        return max(-0x8000_0000, min(0x7FFF_FFFF, int(round(val / lsb))))

    def _sc_i16(val: float, lsb: float) -> int:
        return max(-32768, min(32767, int(round(val / lsb))))

    def _r2s(r: float) -> float:   # radians → semicircles
        return r / math.pi

    try:
        svid      = int(sat["svid"])
        health    = int(float(sat.get("health", 0)))
        ecc       = float(sat.get("eccentricity", 0.0))
        toa_s     = float(sat.get("time_of_applicability(s)", 0.0))
        incl_rad  = float(sat.get("orbital_inclination(rad)", 0.0))
        om_dot    = float(sat.get("rate_of_right_ascen(r/s)", 0.0))
        sqrt_a    = float(sat.get("sqrt(a)__(m_1/2)", sat.get("sqrt(a)_(m_1/2)", 0.0)))
        omega0    = float(sat.get("right_ascen_at_week(rad)", 0.0))
        omega     = float(sat.get("argument_of_perigee(rad)", 0.0))
        m0        = float(sat.get("mean_anom(rad)", 0.0))
        af0       = float(sat.get("af0(s)", 0.0))
        af1       = float(sat.get("af1(s/s)", 0.0))

        payload = struct.pack(
            "<BBBBIIhhIiiihh",
            0x00,                                       # type = GPS almanac
            0x00,                                       # version
            svid,                                       # svId (1-32)
            health & 0xFF,                              # svHealth
            _sc_u(ecc,                   2**-21),       # e (unsigned)
            _sc_u(toa_s,                 4096.0),       # almToa = toa_s / 4096
            _sc_i16(_r2s(incl_rad - _REF_I), 2**-19),  # deltaI (delta from 0.3π)
            _sc_i16(_r2s(om_dot),            2**-38),  # omegaDot
            _sc_u(sqrt_a,                2**-11),       # sqrtA (unsigned)
            _sc_i32(_r2s(omega0),        2**-23),       # omega0
            _sc_i32(_r2s(omega),         2**-23),       # omega
            _sc_i32(_r2s(m0),            2**-23),       # m0
            _sc_i16(af0,                 2**-20),       # af0
            _sc_i16(af1,                 2**-38),       # af1
        )
        return _ubx_frame(0x13, 0x00, payload)
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        log.debug("Skipping svid %s: %s", sat.get("svid"), exc)
        return None


# ── MAVLink injection ─────────────────────────────────────────────────────────

def inject_via_mavlink(port: str, baud: int, messages: list[bytes]) -> None:
    try:
        from pymavlink import mavutil
    except ImportError:
        log.error("pymavlink not installed")
        sys.exit(1)

    log.info("Connecting to MAVLink on %s @ %d …", port, baud)
    conn = mavutil.mavlink_connection(port, baud=baud)
    hb = conn.wait_heartbeat(timeout=15)
    if hb is None:
        log.error("No heartbeat — is the Matek connected?")
        sys.exit(1)
    log.info("Connected  sysid=%d  compid=%d", conn.target_system, conn.target_component)

    total_bytes = sum(len(m) for m in messages)
    sent_bytes  = 0
    sent_msgs   = 0

    for ubx_msg in messages:
        # Chunk into 110-byte GPS_INJECT_DATA payloads
        for offset in range(0, len(ubx_msg), 110):
            chunk = ubx_msg[offset:offset + 110]
            conn.mav.gps_inject_data_send(
                conn.target_system,
                conn.target_component,
                len(chunk),
                list(chunk) + [0] * (110 - len(chunk)),   # zero-pad to 110
            )
            time.sleep(0.01)   # ~100 Hz — don't flood the link
        sent_bytes += len(ubx_msg)
        sent_msgs  += 1
        if sent_msgs % 10 == 0:
            log.info("  … %d / %d messages  (%d bytes)", sent_msgs, len(messages), sent_bytes)

    log.info("Injected %d UBX messages  (%d bytes total)", sent_msgs, sent_bytes)
    conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def find_port() -> str:
    for pattern in ["/dev/ttyACM*", "/dev/ttyUSB*"]:
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return "/dev/ttyACM0"


def main() -> None:
    ap = argparse.ArgumentParser(description="GPS AssistNow-free injection")
    ap.add_argument("--port",  default=None,        help="Serial port (auto-detect if omitted)")
    ap.add_argument("--baud",  default=57600, type=int)
    ap.add_argument("--lat",   default=DEFAULT_LAT,  type=float, help="Approx latitude (deg)")
    ap.add_argument("--lon",   default=DEFAULT_LON,  type=float, help="Approx longitude (deg)")
    ap.add_argument("--alt",   default=DEFAULT_ALT,  type=float, help="Approx altitude (m)")
    ap.add_argument("--no-almanac", action="store_true", help="Skip almanac download")
    args = ap.parse_args()

    port = args.port or find_port()
    log.info("Using port: %s", port)

    ubx_messages: list[bytes] = []

    # 1. Time assistance
    now = datetime.datetime.now(datetime.timezone.utc)
    log.info("Injecting UTC time: %s", now.strftime("%Y-%m-%d %H:%M:%S"))
    ubx_messages.append(build_mga_ini_time(now))

    # 2. Position assistance
    log.info("Injecting approximate position: %.4f°N  %.4f°E  ±%.0f km",
             args.lat, args.lon, args.alt)
    ubx_messages.append(build_mga_ini_pos(args.lat, args.lon, args.alt))

    # 3. Almanac from CelesTrak
    if not args.no_almanac:
        log.info("Downloading almanac from CelesTrak …")
        try:
            req = urllib.request.Request(YUMA_URL, headers={"User-Agent": "SiloController/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                yuma_text = resp.read().decode("ascii", errors="ignore")
            sats = _parse_yuma(yuma_text)
            log.info("Parsed %d satellites from YUMA almanac", len(sats))
            built = 0
            for sat in sats:
                msg = build_mga_gps_alm(sat)
                if msg:
                    ubx_messages.append(msg)
                    built += 1
            log.info("Built %d UBX-MGA-GPS-ALM messages", built)
        except Exception as exc:
            log.warning("Almanac download failed (%s) — continuing with time+pos only", exc)
    else:
        log.info("Almanac skipped (--no-almanac)")

    # Inject everything
    inject_via_mavlink(port, args.baud, ubx_messages)
    log.info("Done — GPS should acquire fix within 30–90 seconds outdoors.")


if __name__ == "__main__":
    main()
