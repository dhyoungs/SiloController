"""
Patrick Blackett Silo 2 Controller — desktop GUI.

Maritime military design.
  Background : #040c18  (deep ocean)
  Cards      : #0a1628  (dark hull)
  Borders    : #1a3a5c  (steel blue)
  Gold       : #c89b2a  (naval brass)
  Cyan       : #00c8f0  (radar readout)
  Green      : #00c870  (active)
  Red        : #c83030  (alert)
  Amber      : #e07820  (transition)

Layout  980 × 580
-----------------
  Top bar  : title  |  IP  |  uptime
  Left col : Silo Lid card  +  Sea Trials Data Recording card
  Right col: Navigation strip  |  Motion Analysis table  |  Attitude + Compass
"""

import math
import os
import socket
import subprocess
import sys
import time
import tkinter as tk
from tkinter import font as tkfont
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Palette ───────────────────────────────────────────────────────────────────
BG     = "#040c18"
CARD   = "#0a1628"
BORDER = "#1a3a5c"
GOLD   = "#c89b2a"
CYAN   = "#00c8f0"
GREEN  = "#00c870"
RED    = "#c83030"
AMBER  = "#e07820"
FG     = "#c0d0e0"
FG_DIM = "#3a5a7a"
FG_MUT = "#6080a0"
WHITE  = "#e0f0ff"

STATE_COLOUR = {
    "closed": RED, "opening": AMBER, "open": GREEN, "closing": AMBER
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "—"

def _hw_uptime_s() -> int:
    """Seconds since the Pi last booted (reads /proc/uptime)."""
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return 0

def _fmt_hms(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def _card(parent, **kw) -> tk.Frame:
    return tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                    highlightthickness=1, **kw)

def _lbl(parent, text="", fg=FG, font=None, anchor="w", **kw) -> tk.Label:
    return tk.Label(parent, text=text, fg=fg, bg=parent["bg"],
                    font=font, anchor=anchor, **kw)

def _btn(parent, text, bg, fg, cmd, w=9) -> tk.Button:
    return tk.Button(
        parent, text=text, width=w,
        bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
        font=("Courier", 9, "bold"), relief="flat", cursor="hand2",
        command=cmd,
    )


# ── Compass widget ────────────────────────────────────────────────────────────

class CompassWidget:
    SIZE = 110
    def __init__(self, parent):
        self.canvas = tk.Canvas(parent, width=self.SIZE, height=self.SIZE,
                                bg=CARD, highlightthickness=0)
        self._draw(0.0)

    def update(self, hdg: float) -> None:
        self._draw(hdg)

    def _draw(self, hdg: float) -> None:
        c = self.canvas; c.delete("all")
        cx = cy = self.SIZE // 2; r = cx - 4
        c.create_oval(cx-r, cy-r, cx+r, cy+r, outline=GOLD, width=1, fill=BG)
        for label, deg in (("N",0),("E",90),("S",180),("W",270)):
            a = math.radians(deg - 90)
            c.create_text(cx+(r-13)*math.cos(a), cy+(r-13)*math.sin(a),
                          text=label, fill=GOLD, font=("Courier", 8, "bold"))
        for deg in range(0, 360, 30):
            a = math.radians(deg - 90)
            ln = 9 if deg % 90 == 0 else 5
            c.create_line(cx+(r-ln)*math.cos(a), cy+(r-ln)*math.sin(a),
                          cx+r*math.cos(a),       cy+r*math.sin(a),
                          fill=BORDER, width=1)
        a   = math.radians(hdg - 90)
        pa  = a + math.pi/2
        tip = (cx+(r-18)*math.cos(a), cy+(r-18)*math.sin(a))
        bas = (cx-10*math.cos(a),     cy-10*math.sin(a))
        c.create_polygon(tip[0], tip[1],
                         bas[0]-6*math.cos(pa), bas[1]-6*math.sin(pa),
                         bas[0]+6*math.cos(pa), bas[1]+6*math.sin(pa),
                         fill=CYAN, outline="")
        c.create_text(cx, cy+2, text=f"{hdg:.0f}°",
                      fill=WHITE, font=("Courier", 9, "bold"))


# ── Attitude / motion widget ──────────────────────────────────────────────────

class MotionWidget:
    """Artificial horizon tuned for nautical use (sky=midnight blue, sea=dark teal)."""
    SIZE = 110
    def __init__(self, parent):
        self.canvas = tk.Canvas(parent, width=self.SIZE, height=self.SIZE,
                                bg=CARD, highlightthickness=0)
        self.update(0.0, 0.0)

    def update(self, pitch_deg: float, roll_deg: float) -> None:
        c = self.canvas; c.delete("all")
        cx = cy = self.SIZE // 2; r = cx - 3
        rrr = math.radians(roll_deg)
        pitch_px = max(min(pitch_deg * r / 45.0, r), -r)
        hcx = cx + math.sin(rrr) * pitch_px
        hcy = cy + math.cos(rrr) * pitch_px

        # Sky
        c.create_oval(cx-r, cy-r, cx+r, cy+r, fill="#0a1e3a", outline="")
        # Sea (ground polygon)
        hw = r * 3.5
        cos_r, sin_r = math.cos(rrr), math.sin(rrr)
        flat = []
        for lx, ly in [(-hw,0),(hw,0),(hw,hw*2),(-hw,hw*2)]:
            flat += [cos_r*lx - sin_r*ly + hcx, sin_r*lx + cos_r*ly + hcy]
        c.create_polygon(flat, fill="#0a2a2a", outline="")
        # Ring mask
        rr2 = r + 50
        c.create_oval(cx-rr2, cy-rr2, cx+rr2, cy+rr2, outline=CARD, width=100, fill="")
        # Horizon
        ext = r + 2
        c.create_line(hcx-ext*cos_r, hcy-ext*sin_r,
                      hcx+ext*cos_r, hcy+ext*sin_r,
                      fill=CYAN, width=1)
        # Border
        c.create_oval(cx-r, cy-r, cx+r, cy+r, outline=GOLD, width=1, fill="")
        # Vessel reference
        c.create_line(cx-26, cy, cx-8, cy, fill=GOLD, width=2)
        c.create_line(cx+ 8, cy, cx+26, cy, fill=GOLD, width=2)
        c.create_oval(cx-3, cy-3, cx+3, cy+3, fill=GOLD, outline="")
        # Roll tick
        ta = math.radians(-roll_deg) - math.pi/2
        ir = r - 7; tx = cx+ir*math.cos(ta); ty = cy+ir*math.sin(ta)
        pa = ta + math.pi/2
        c.create_polygon(tx, ty,
                         tx+5*math.cos(pa), ty+5*math.sin(pa),
                         tx-5*math.cos(pa), ty-5*math.sin(pa),
                         fill=GOLD)


# ── Main GUI ──────────────────────────────────────────────────────────────────

class SiloGUI:
    _WINDOWS = ["current", "1m", "5m", "10m", "30m"]
    _METRICS = [
        ("speed",      "SPEED",      "kt"),
        ("pitch",      "PITCH",      "°"),
        ("pitch_rate", "PITCH RT",   "°/s"),
        ("roll",       "ROLL",       "°"),
        ("roll_rate",  "ROLL RT",    "°/s"),
        ("yaw_rate",   "YAW RT",     "°/s"),
    ]

    def __init__(self, silo, telemetry, recorder, stats):
        self._silo      = silo
        self._telem     = telemetry
        self._recorder  = recorder
        self._stats     = stats
        self._root: tk.Tk | None = None
        self._start     = time.monotonic()
        self._ip        = _get_ip()
        self._stat_vars: dict[tuple, tk.StringVar] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("Patrick Blackett Silo 2 Controller")
        root.geometry("980x580")
        root.configure(bg=BG)
        root.resizable(False, False)

        self._build_fonts()
        self._build_ui(root)

        self._silo.add_listener(self._on_silo_state)
        self._recorder.add_listener(self._on_recording_change)

        self._apply_silo_state(self._silo.state)
        self._apply_recording(self._recorder.active)
        self._poll_telemetry()
        self._tick_statusbar()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.mainloop()

    # ── Fonts ──────────────────────────────────────────────────────────────────

    def _build_fonts(self) -> None:
        self.f_title  = tkfont.Font(family="Courier", size=13, weight="bold")
        self.f_head   = tkfont.Font(family="Courier", size=8,  weight="bold")
        self.f_state  = tkfont.Font(family="Courier", size=11, weight="bold")
        self.f_val    = tkfont.Font(family="Courier", size=10, weight="bold")
        self.f_nav    = tkfont.Font(family="Courier", size=9,  weight="bold")
        self.f_stat   = tkfont.Font(family="Courier", size=9,  weight="bold")
        self.f_small  = tkfont.Font(family="Courier", size=8)
        self.f_status = tkfont.Font(family="Courier", size=8)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self, root: tk.Tk) -> None:
        # ── Top bar ────────────────────────────────────────────────────────
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 0))

        title_f = tk.Frame(top, bg=BG)
        title_f.pack(side="left")
        tk.Label(title_f, text="PATRICK BLACKETT", font=self.f_title,
                 fg=GOLD, bg=BG).pack(side="left")
        tk.Label(title_f, text="  SILO 2 CONTROLLER", font=self.f_title,
                 fg=WHITE, bg=BG).pack(side="left")

        # Restart button (far right)
        _btn(top, "RESTART", AMBER, "#1a0800", self._cmd_restart, w=8).pack(
            side="right", padx=(10, 0))

        self._lbl_utc    = tk.Label(top, text="UTC: --:--:--",
                                    font=self.f_status, fg=FG_MUT, bg=BG)
        self._lbl_utc.pack(side="right", padx=(10, 0))
        self._lbl_hwup   = tk.Label(top, text="HW UP: 00:00:00",
                                    font=self.f_status, fg=FG_MUT, bg=BG)
        self._lbl_hwup.pack(side="right", padx=(10, 0))
        self._lbl_uptime = tk.Label(top, text="SW UP: 00:00:00",
                                    font=self.f_status, fg=CYAN, bg=BG)
        self._lbl_uptime.pack(side="right", padx=(10, 0))
        self._lbl_ip = tk.Label(top, text=f"IP: {self._ip}",
                                font=self.f_status, fg=FG_MUT, bg=BG)
        self._lbl_ip.pack(side="right", padx=(10, 0))

        # Divider
        tk.Frame(root, bg=GOLD, height=1).pack(fill="x", padx=10, pady=(6, 4))

        # ── Body ───────────────────────────────────────────────────────────
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=8, pady=0)

        left  = tk.Frame(body, bg=BG, width=215)
        right = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 8))
        right.pack(side="left", fill="both", expand=True)
        left.pack_propagate(False)

        self._build_silo_card(left)
        self._build_record_card(left)
        self._build_nav_card(right)
        self._build_stats_card(right)
        self._build_vis_row(right)

        # Log line
        self._lbl_log = tk.Label(root, text="", font=self.f_status,
                                 fg=FG_DIM, bg=BG, anchor="e")
        self._lbl_log.pack(fill="x", padx=10, pady=(2, 4))

    # ── Left panel ─────────────────────────────────────────────────────────────

    def _build_silo_card(self, parent: tk.Frame) -> None:
        c = _card(parent); c.pack(fill="x", pady=(0, 6))
        _lbl(c, "— SILO LID —", GOLD, self.f_head).pack(anchor="w", padx=8, pady=(6, 4))

        row = tk.Frame(c, bg=CARD); row.pack(anchor="w", padx=8, pady=(0, 4))
        self._dot_silo = tk.Canvas(row, width=12, height=12, bg=CARD, highlightthickness=0)
        self._dot_silo.pack(side="left", padx=(0, 6))
        self._oval_silo = self._dot_silo.create_oval(1,1,11,11, fill=RED, outline="")
        self._lbl_silo = _lbl(row, "CLOSED", CYAN, self.f_state, width=8)
        self._lbl_silo.pack(side="left")

        bb = tk.Frame(c, bg=CARD); bb.pack(padx=8, pady=(0, 8))
        self._btn_open  = _btn(bb, "OPEN",  GREEN, "#001a0a", self._cmd_open)
        self._btn_close = _btn(bb, "CLOSE", RED,   "#1a0000", self._cmd_close)
        self._btn_open.pack(side="left", padx=(0, 4))
        self._btn_close.pack(side="left")

    def _build_record_card(self, parent: tk.Frame) -> None:
        c = _card(parent); c.pack(fill="x")
        _lbl(c, "— SEA TRIALS —", GOLD, self.f_head).pack(anchor="w", padx=8, pady=(6, 2))
        _lbl(c, "DATA RECORDING", GOLD, self.f_head).pack(anchor="w", padx=8, pady=(0, 4))

        row = tk.Frame(c, bg=CARD); row.pack(anchor="w", padx=8, pady=(0, 4))
        self._dot_rec = tk.Canvas(row, width=12, height=12, bg=CARD, highlightthickness=0)
        self._dot_rec.pack(side="left", padx=(0, 6))
        self._oval_rec = self._dot_rec.create_oval(1,1,11,11, fill=FG_DIM, outline="")
        self._lbl_rec = _lbl(row, "IDLE", CYAN, self.f_state, width=8)
        self._lbl_rec.pack(side="left")

        bb = tk.Frame(c, bg=CARD); bb.pack(padx=8, pady=(0, 8))
        self._btn_rec_start = _btn(bb, "START", GREEN, "#001a0a", self._cmd_rec_start)
        self._btn_rec_stop  = _btn(bb, "STOP",  RED,   "#1a0000", self._cmd_rec_stop)
        self._btn_rec_start.pack(side="left", padx=(0, 4))
        self._btn_rec_stop.pack(side="left")

    # ── Right panel ────────────────────────────────────────────────────────────

    def _build_nav_card(self, parent: tk.Frame) -> None:
        c = _card(parent); c.pack(fill="x", pady=(0, 4))
        _lbl(c, "— NAVIGATION —", GOLD, self.f_head).pack(anchor="w", padx=8, pady=(4, 4))

        g = tk.Frame(c, bg=CARD); g.pack(padx=8, pady=(0, 6))
        self._nval: dict[str, tk.StringVar] = {}
        fields = [
            ("LAT",  "lat",   0, 0), ("LON",  "lon",   0, 2),
            ("ALT",  "alt",   0, 4), ("GPS",  "gps",   0, 6),
            ("COG",  "cog",   1, 0), ("SPD",  "spd",   1, 2),
            ("PITCH","pitch", 1, 4), ("ROLL", "roll",  1, 6),
        ]
        for name, key, row, col in fields:
            _lbl(g, name, FG_DIM, self.f_small, anchor="e").grid(
                row=row, column=col, sticky="e", padx=(6, 2), pady=1)
            v = tk.StringVar(value="—")
            self._nval[key] = v
            tk.Label(g, textvariable=v, font=self.f_nav, fg=CYAN, bg=CARD,
                     anchor="w", width=11).grid(row=row, column=col+1, sticky="w", pady=1)

    def _build_stats_card(self, parent: tk.Frame) -> None:
        c = _card(parent); c.pack(fill="x", pady=(0, 4))
        _lbl(c, "— MOTION ANALYSIS —", GOLD, self.f_head).pack(anchor="w", padx=8, pady=(4, 4))

        g = tk.Frame(c, bg=CARD); g.pack(padx=8, pady=(0, 6))

        # Column headers: current | 1m (min/avg/max) | 5m | 10m | 30m
        hdr_labels = ["", "CURRENT", "1m min/avg/max", "5m min/avg/max",
                       "10m min/avg/max", "30m min/avg/max"]
        for col, h in enumerate(hdr_labels):
            tk.Label(g, text=h, font=self.f_small, fg=FG_DIM, bg=CARD,
                     width=15 if col > 1 else (13 if col == 0 else 9),
                     anchor="center").grid(row=0, column=col, padx=2, pady=(0, 2))

        win_keys = ["current", "1m", "5m", "10m", "30m"]
        for row_i, (key, label, unit) in enumerate(self._METRICS, start=1):
            lbl = f"{label} ({unit})"
            tk.Label(g, text=lbl, font=self.f_stat, fg=GOLD, bg=CARD,
                     anchor="w", width=13).grid(row=row_i, column=0, padx=(0, 4), pady=1)
            for col_i, win in enumerate(win_keys, start=1):
                v = tk.StringVar(value="—")
                self._stat_vars[(key, win)] = v
                colour = WHITE if win == "current" else CYAN
                width  = 9 if win == "current" else 15
                tk.Label(g, textvariable=v, font=self.f_stat, fg=colour, bg=CARD,
                         width=width, anchor="center").grid(row=row_i, column=col_i, pady=1)

    def _build_vis_row(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=BG); row.pack(fill="x", pady=(0, 0))

        for title, widget_attr, WidgetClass in [
            ("MOTION",  "_motion_w",  MotionWidget),
            ("BEARING", "_compass_w", CompassWidget),
        ]:
            card = _card(row); card.pack(side="left", padx=(0, 6))
            _lbl(card, f"— {title} —", GOLD, self.f_head).pack(anchor="w", padx=8, pady=(4, 2))
            w = WidgetClass(card)
            w.canvas.pack(padx=8, pady=(0, 6))
            setattr(self, widget_attr, w)

        # Quick-read card
        qr = _card(row); qr.pack(side="left", fill="y")
        _lbl(qr, "— QUICK READ —", GOLD, self.f_head).pack(anchor="w", padx=8, pady=(4, 4))
        self._qr_vars: dict[str, tk.StringVar] = {}
        for label in ("PITCH", "ROLL", "YAW RT", "SPEED"):
            r2 = tk.Frame(qr, bg=CARD); r2.pack(anchor="w", padx=8, pady=1)
            _lbl(r2, f"{label:<8}", FG_DIM, self.f_small).pack(side="left")
            v = tk.StringVar(value="—")
            self._qr_vars[label] = v
            tk.Label(r2, textvariable=v, font=self.f_nav, fg=CYAN, bg=CARD).pack(side="left")
        qr.pack_configure(pady=(0, 4))

    # ── Commands ───────────────────────────────────────────────────────────────

    def _cmd_open(self) -> None:
        r = self._silo.open(source="gui")
        if not r["ok"]: self._log(f"OPEN REJECTED: {r['reason']}")

    def _cmd_close(self) -> None:
        r = self._silo.close(source="gui")
        if not r["ok"]: self._log(f"CLOSE REJECTED: {r['reason']}")

    def _cmd_restart(self) -> None:
        logger.info("Restart requested from GUI")
        self._silo.cleanup()
        if self._root:
            self._root.destroy()
        # Exit cleanly — systemd Restart=always brings the process back up
        os.kill(os.getpid(), 15)

    def _cmd_rec_start(self) -> None:
        r = self._recorder.start_recording(source="gui")
        if not r["ok"]: self._log(f"RECORD REJECTED: {r['reason']}")

    def _cmd_rec_stop(self) -> None:
        r = self._recorder.stop_recording(source="gui")
        if not r["ok"]: self._log(f"RECORD REJECTED: {r['reason']}")

    # ── Silo state ─────────────────────────────────────────────────────────────

    def _on_silo_state(self, state: str) -> None:
        if self._root: self._root.after(0, self._apply_silo_state, state)

    def _apply_silo_state(self, state: str) -> None:
        col = STATE_COLOUR.get(state, FG_DIM)
        self._dot_silo.itemconfig(self._oval_silo, fill=col)
        self._lbl_silo.config(text=state.upper())
        self._btn_open.config(state="disabled" if state in ("open","opening") else "normal")
        self._btn_close.config(state="disabled" if state in ("closed","closing") else "normal")
        self._log(f"SILO → {state.upper()}")
        if state in ("opening", "closing"):
            self._pulse_silo(True)

    def _pulse_silo(self, hi: bool) -> None:
        if self._silo.state not in ("opening", "closing"): return
        self._dot_silo.itemconfig(self._oval_silo, fill=AMBER if hi else BORDER)
        if self._root: self._root.after(500, self._pulse_silo, not hi)

    # ── Recording ──────────────────────────────────────────────────────────────

    def _on_recording_change(self, active: bool) -> None:
        if self._root: self._root.after(0, self._apply_recording, active)

    def _apply_recording(self, active: bool) -> None:
        if active:
            self._dot_rec.itemconfig(self._oval_rec, fill=RED)
            self._lbl_rec.config(text="ACTIVE")
            self._btn_rec_start.config(state="disabled")
            self._btn_rec_stop.config(state="normal")
            self._pulse_rec(True)
        else:
            self._dot_rec.itemconfig(self._oval_rec, fill=FG_DIM)
            self._lbl_rec.config(text="IDLE")
            self._btn_rec_start.config(state="normal")
            self._btn_rec_stop.config(state="disabled")

    def _pulse_rec(self, hi: bool) -> None:
        if not self._recorder.active: return
        self._dot_rec.itemconfig(self._oval_rec, fill=RED if hi else BORDER)
        if self._root: self._root.after(500, self._pulse_rec, not hi)

    # ── Telemetry poll (5 Hz) ──────────────────────────────────────────────────

    def _poll_telemetry(self) -> None:
        f = self._telem.frame
        s = self._stats.get(f)

        if f.valid:
            kts = f.groundspeed * 1.94384
            fix_str = {0:"NO FIX",1:"NO FIX",2:"2D",3:"3D",6:"RTK"}.get(f.gps_fix, "?")
            self._nval["lat"].set(f"{f.lat:+.5f}°")
            self._nval["lon"].set(f"{f.lon:+.6f}°")
            self._nval["alt"].set(f"{f.alt_m:.1f} m")
            self._nval["gps"].set(f"{fix_str} {f.satellites}sat")
            self._nval["cog"].set(f"{f.heading_deg:.1f}°")
            self._nval["spd"].set(f"{kts:.2f} kt")
            self._nval["pitch"].set(f"{f.pitch_deg:+.1f}°")
            self._nval["roll"].set(f"{f.roll_deg:+.1f}°")

        # Stats table — current shows single value; windows show min/avg/max
        cur = s["current"]
        for key, _, _ in self._METRICS:
            self._stat_vars[(key, "current")].set(f"{cur.get(key, 0.0):.1f}")
            for win in ("1m", "5m", "10m", "30m"):
                d = s[win].get(key, {})
                lo  = d.get("min", 0.0)
                avg = d.get("avg", 0.0)
                hi  = d.get("max", 0.0)
                self._stat_vars[(key, win)].set(f"{lo:.1f}/{avg:.1f}/{hi:.1f}")

        # Quick-read panel
        self._qr_vars["PITCH"].set(f"{f.pitch_deg:+.1f}°")
        self._qr_vars["ROLL"].set(f"{f.roll_deg:+.1f}°")
        self._qr_vars["YAW RT"].set(f"{cur['yaw_rate']:.1f}°/s")
        self._qr_vars["SPEED"].set(f"{cur['speed']:.1f} kt")

        self._motion_w.update(f.pitch_deg, f.roll_deg)
        self._compass_w.update(f.yaw_deg)

        if self._root:
            self._root.after(200, self._poll_telemetry)

    # ── Status bar tick (1 Hz) ────────────────────────────────────────────────

    def _tick_statusbar(self) -> None:
        elapsed = int(time.monotonic() - self._start)
        self._lbl_uptime.config(text=f"SW UP: {_fmt_hms(elapsed)}")
        self._lbl_hwup.config(text=f"HW UP: {_fmt_hms(_hw_uptime_s())}")
        self._lbl_utc.config(
            text="UTC: " + datetime.now(timezone.utc).strftime("%H:%M:%S"))
        # Refresh IP every 10 seconds to catch DHCP changes
        if elapsed % 10 == 0:
            new_ip = _get_ip()
            if new_ip != self._ip:
                self._ip = new_ip
                self._lbl_ip.config(text=f"IP: {self._ip}")
        if self._root:
            self._root.after(1000, self._tick_statusbar)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        self._lbl_log.config(text=msg)

    def _on_close(self) -> None:
        self._silo.cleanup()
        if self._root: self._root.destroy()
