"""System info for the standard project status bar.

Matches the LAMT reference implementation so every web UI on this Pi
behaves identically: `/api/stats` returns pre-formatted strings the
template can drop straight into the bar.
"""

from __future__ import annotations

import datetime as dt
import shutil
import socket
import subprocess
import time


APP_STARTED = time.time()


def _lan_ip() -> str:
    """Best-effort LAN IPv4. Uses a UDP trick that doesn't actually send traffic."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            return subprocess.check_output(["hostname", "-I"], text=True).split()[0]
        except Exception:
            return "?"


def _pi_uptime_s() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _meminfo() -> dict:
    info: dict = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0].strip()] = int(parts[1].strip().split()[0]) * 1024
    except Exception:
        pass
    return info


def _fmt_dur(s: float) -> str:
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _fmt_bytes(n: float) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return f"{n:.1f}{unit}"
        n /= step
    return f"{n:.1f}PB"


def stats(port: int) -> dict:
    mem = _meminfo()
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    du = shutil.disk_usage("/")
    return {
        "utc": dt.datetime.utcnow().strftime("%H:%M:%S"),
        "ip": _lan_ip(),
        "port": port,
        "pi_uptime": _fmt_dur(_pi_uptime_s()),
        "app_uptime": _fmt_dur(time.time() - APP_STARTED),
        "ram_used": _fmt_bytes(mem_total - mem_avail),
        "ram_total": _fmt_bytes(mem_total),
        "disk_used": _fmt_bytes(du.used),
        "disk_total": _fmt_bytes(du.total),
    }
