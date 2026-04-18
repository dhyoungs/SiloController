"""System info for the standard project status bar.

Matches the LAMT reference implementation (progress-bar / percent
variant) so every web UI on this Pi behaves identically: `/api/stats`
returns pre-formatted text plus a percent value, and the template
renders a coloured gauge for RAM and disk.
"""

from __future__ import annotations

import datetime as dt
import shutil
import socket
import subprocess
import time


APP_STARTED = time.time()


def _lan_ip() -> str:
    """Best-effort LAN IPv4. UDP connect trick that doesn't actually send."""
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


def _fmt_used_total(used: int, total: int) -> tuple[str, int]:
    """Return ('<used>/<total> <unit>', percent) — same unit for both sides."""
    if total <= 0:
        return ("0/0", 0)
    step = 1024.0
    units = ("B", "KB", "MB", "GB", "TB")
    u = float(used)
    t = float(total)
    idx = 0
    while t >= step and idx < len(units) - 1:
        u /= step
        t /= step
        idx += 1
    pct = round((used / total) * 100)
    return (f"{u:.1f}/{t:.1f} {units[idx]}", pct)


def stats(port: int) -> dict:
    mem = _meminfo()
    mem_total = mem.get("MemTotal", 0)
    mem_used = mem_total - mem.get("MemAvailable", 0)
    du = shutil.disk_usage("/")
    ram_text, ram_pct = _fmt_used_total(mem_used, mem_total)
    disk_text, disk_pct = _fmt_used_total(du.used, du.total)
    return {
        "utc": dt.datetime.utcnow().strftime("%H:%M:%S"),
        "ip": _lan_ip(),
        "port": port,
        "pi_uptime": _fmt_dur(_pi_uptime_s()),
        "app_uptime": _fmt_dur(time.time() - APP_STARTED),
        "ram": ram_text, "ram_pct": ram_pct,
        "disk": disk_text, "disk_pct": disk_pct,
    }
