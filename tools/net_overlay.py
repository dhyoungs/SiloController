#!/usr/bin/env python3
"""
net_overlay.py — Burns active interface/IP info into the desktop wallpaper.

Strategy
--------
1. Detect whether a Wayland or X11 display is available; exit silently if not.
2. Query screen resolution and physical dimensions to derive DPI.
3. Load the source wallpaper (from pcmanfm config or the RPD default).
4. Composite a semi-transparent text box into the top-left corner.
5. Save the result to ~/.config/net-wallpaper.png.
6. Write a [desktop] section to the user pcmanfm config pointing at that file.
7. Signal pcmanfm to reload.

Run this script on a timer (systemd) — it is safe to call repeatedly.
"""

import configparser
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_WALLPAPER_FALLBACK = "/usr/share/rpd-wallpaper/RPiSystem.png"
USER_WALLPAPER = Path.home() / ".config" / "net-wallpaper.png"
PCMANFM_CONF = Path.home() / ".config" / "pcmanfm" / "default" / "pcmanfm.conf"
FONT_PATH = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"

# Physical text size target in millimetres (constant across resolutions)
TEXT_HEIGHT_MM = 6.0        # height of each text line
BOX_PADDING_MM = 3.75       # internal padding inside the box
BOX_MARGIN_MM  = 12.0       # margin from screen edge to box

TEXT_COLOR   = (0, 255, 136)        # bright green
BOX_COLOR    = (0, 0, 0, 180)       # black, semi-transparent
SHADOW_COLOR = (0, 0, 0, 220)       # drop-shadow tint


# ---------------------------------------------------------------------------
# Display detection
# ---------------------------------------------------------------------------

def display_available() -> bool:
    """Return True if a live Wayland or X11 session is reachable."""
    if os.environ.get("WAYLAND_DISPLAY"):
        sock = Path(f"/run/user/{os.getuid()}") / os.environ["WAYLAND_DISPLAY"]
        return sock.exists()
    if os.environ.get("DISPLAY"):
        return True
    return False


# ---------------------------------------------------------------------------
# Screen geometry
# ---------------------------------------------------------------------------

def screen_geometry() -> tuple[int, int, float, float]:
    """
    Return (width_px, height_px, width_mm, height_mm) for the primary output.
    Falls back to 96 DPI assumptions if detection fails.
    """
    # Try wlr-randr (Wayland)
    # Output format (per output block):
    #   Physical size: 620x340 mm
    #     3840x2160 px, 30.00 Hz (preferred, current)   ← active mode
    if os.environ.get("WAYLAND_DISPLAY"):
        try:
            r = subprocess.run(
                ["wlr-randr"],
                capture_output=True, text=True, timeout=4,
                env={**os.environ, "WAYLAND_DISPLAY": os.environ["WAYLAND_DISPLAY"]},
            )
            w_mm = h_mm = 0.0
            for line in r.stdout.splitlines():
                line_s = line.strip()
                # Physical size: 620x340 mm
                if line_s.startswith("Physical size:"):
                    parts = line_s.split(":")[1].strip().split()
                    dims  = parts[0].split("x")
                    w_mm, h_mm = float(dims[0]), float(dims[1])
                # "3840x2160 px, ... (preferred, current)"
                if "current" in line_s and "px," in line_s:
                    res = line_s.split()[0].split("x")
                    w_px, h_px = int(res[0]), int(res[1])
                    if w_px > 0 and h_px > 0 and w_mm > 0 and h_mm > 0:
                        return w_px, h_px, w_mm, h_mm
        except Exception:
            pass

    # Try xrandr (X11 fallback)
    if os.environ.get("DISPLAY"):
        try:
            r = subprocess.run(["xrandr", "--query"],
                               capture_output=True, text=True, timeout=4)
            for line in r.stdout.splitlines():
                if " connected" in line:
                    # e.g. "HDMI-A-1 connected 3840x2160+0+0 ... 620mm x 340mm"
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if "x" in p and "+" in p:
                            res = p.split("+")[0].split("x")
                            w_px, h_px = int(res[0]), int(res[1])
                        if p.endswith("mm") and i > 0:
                            try:
                                w_mm = int(parts[i - 2])
                                h_mm = int(p.rstrip("mm"))
                                if w_px and h_px and w_mm > 0 and h_mm > 0:
                                    return w_px, h_px, float(w_mm), float(h_mm)
                            except ValueError:
                                pass
        except Exception:
            pass

    # Ultimate fallback: assume 96 DPI on 1920×1080
    return 1920, 1080, 507.0, 285.0


def dpi(w_px: int, _h_px: int, w_mm: float, _h_mm: float) -> float:
    return w_px / (w_mm / 25.4)


def mm_to_px(mm: float, dpi_val: float) -> int:
    return max(1, round(mm * dpi_val / 25.4))


# ---------------------------------------------------------------------------
# Network info
# ---------------------------------------------------------------------------

def get_net_lines() -> list[str]:
    lines = []
    try:
        r = subprocess.run(["ip", "-br", "addr", "show"],
                           capture_output=True, text=True, timeout=3)
        for row in r.stdout.splitlines():
            parts = row.split()
            if len(parts) < 2:
                continue
            iface, state = parts[0], parts[1]
            if iface == "lo" or state not in ("UP", "UNKNOWN"):
                continue
            addrs = [p for p in parts[2:]
                     if "." in p and not p.startswith("169.254")]
            ip4 = addrs[0].split("/")[0] if addrs else "no IPv4"
            lines.append(f"{iface:<8}  {ip4}")
    except Exception as exc:
        lines.append(f"err: {exc}")

    if not lines:
        lines.append("no active interface")

    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"

    return [hostname] + lines


# ---------------------------------------------------------------------------
# Source wallpaper path
# ---------------------------------------------------------------------------

def source_wallpaper() -> str:
    cfg = configparser.RawConfigParser()
    cfg.read(str(PCMANFM_CONF))

    # Check user config first (may already point at our generated file)
    for section in ("desktop",):
        if cfg.has_section(section):
            wp = cfg.get(section, "wallpaper", fallback="")
            # Don't use our own generated file as the source
            if wp and wp != str(USER_WALLPAPER) and Path(wp).exists():
                return wp

    # Check system config
    sys_cfg = configparser.RawConfigParser()
    sys_cfg.read("/etc/xdg/pcmanfm/default/pcmanfm.conf")
    if sys_cfg.has_section("desktop"):
        wp = sys_cfg.get("desktop", "wallpaper", fallback="")
        if wp and Path(wp).exists():
            return wp

    return SOURCE_WALLPAPER_FALLBACK


# ---------------------------------------------------------------------------
# Image compositing
# ---------------------------------------------------------------------------

def build_wallpaper(lines: list[str], w_px: int, h_px: int,
                    dpi_val: float) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font_px  = mm_to_px(TEXT_HEIGHT_MM, dpi_val)
    pad_px   = mm_to_px(BOX_PADDING_MM, dpi_val)
    margin   = mm_to_px(BOX_MARGIN_MM,  dpi_val)

    try:
        font = ImageFont.truetype(FONT_PATH, font_px)
    except Exception:
        font = ImageFont.load_default()

    # Measure text
    dummy = Image.new("RGB", (1, 1))
    dd = ImageDraw.Draw(dummy)
    line_heights = []
    line_widths  = []
    for line in lines:
        bb = dd.textbbox((0, 0), line, font=font)
        line_widths.append(bb[2] - bb[0])
        line_heights.append(bb[3] - bb[1])

    line_h   = max(line_heights) if line_heights else font_px
    box_w    = max(line_widths)  + pad_px * 2
    box_h    = line_h * len(lines) + pad_px * 2 + int(line_h * 0.4) * (len(lines) - 1)

    # Load and resize source wallpaper to target resolution
    src = source_wallpaper()
    try:
        img = Image.open(src).convert("RGBA")
    except Exception:
        img = Image.new("RGBA", (w_px, h_px), (30, 30, 30, 255))

    # Crop/resize to fill screen (maintain aspect ratio, crop centre)
    src_w, src_h = img.size
    scale = max(w_px / src_w, h_px / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w_px) // 2
    top  = (new_h - h_px) // 2
    img = img.crop((left, top, left + w_px, top + h_px))

    # Draw semi-transparent box
    overlay = Image.new("RGBA", (w_px, h_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    x0, y0 = margin, margin
    x1, y1 = margin + box_w, margin + box_h
    draw.rounded_rectangle([x0, y0, x1, y1], radius=pad_px,
                            fill=BOX_COLOR)

    # Draw text lines
    line_spacing = int(line_h * 0.4)
    y_text = y0 + pad_px
    for i, line in enumerate(lines):
        # Drop shadow
        draw.text((x0 + pad_px + 2, y_text + 2), line,
                  font=font, fill=(0, 0, 0, 200))
        # Main text
        draw.text((x0 + pad_px, y_text), line,
                  font=font, fill=TEXT_COLOR)
        y_text += line_h + (line_spacing if i < len(lines) - 1 else 0)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    img.save(str(USER_WALLPAPER), "PNG", optimize=False)


# ---------------------------------------------------------------------------
# Update pcmanfm config
# ---------------------------------------------------------------------------

def update_pcmanfm_config() -> None:
    cfg = configparser.RawConfigParser()
    cfg.read(str(PCMANFM_CONF))
    if not cfg.has_section("desktop"):
        cfg.add_section("desktop")
    cfg.set("desktop", "wallpaper",      str(USER_WALLPAPER))
    cfg.set("desktop", "wallpaper_mode", "crop")
    with open(PCMANFM_CONF, "w") as fh:
        cfg.write(fh)


def reload_pcmanfm() -> None:
    env = {**os.environ}
    if "WAYLAND_DISPLAY" not in env:
        env["WAYLAND_DISPLAY"] = "wayland-0"
    subprocess.run(["pcmanfm", "--reconfigure"],
                   env=env, timeout=5,
                   capture_output=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Bail out silently if there is no display — do not crash the Pi
    if not display_available():
        sys.exit(0)

    try:
        w_px, h_px, w_mm, h_mm = screen_geometry()
        dpi_val = dpi(w_px, h_px, w_mm, h_mm)
        lines = get_net_lines()
        build_wallpaper(lines, w_px, h_px, dpi_val)
        update_pcmanfm_config()
        reload_pcmanfm()
    except Exception as exc:
        # Never crash — just log to stderr and exit cleanly
        print(f"net_overlay: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
