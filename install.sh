#!/usr/bin/env bash
# =============================================================================
# Patrick Blackett Silo 2 Controller — Install Script
# =============================================================================
# Run this on a freshly-imaged Raspberry Pi OS (64-bit) with username 'pi'.
#
# Usage:
#   sudo bash install.sh
#
# What this script does:
#   1. Installs system packages (ffmpeg, mosquitto, picamera2, opencv, etc.)
#   2. Copies application code to /home/pi/SiloController
#   3. Creates a Python virtual environment and installs pip packages
#   4. Writes the systemd service unit
#   5. Enables mosquitto and the silo service to start on boot
#   6. Starts both services immediately
# =============================================================================

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}=== $* ===${NC}"; }

# ── Root check ──────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    die "Please run as root: sudo bash install.sh"
fi

# ── Locate the directory that contains this script ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/home/pi/SiloController"
SERVICE_NAME="silocontroller"
VENV="$INSTALL_DIR/.venv"
PI_USER="pi"
PI_HOME="/home/pi"

echo ""
echo -e "${BOLD}Patrick Blackett Silo 2 Controller — Installer${NC}"
echo "  Source  : $SCRIPT_DIR"
echo "  Target  : $INSTALL_DIR"
echo ""

# ── Step 1: System packages ─────────────────────────────────────────────────
section "System packages"

info "Updating apt cache…"
apt-get update -qq

APT_PACKAGES=(
    python3
    python3-venv
    python3-pip
    python3-opencv          # cv2 — binary extension, easiest via apt on Pi
    python3-picamera2       # picamera2 — requires libcamera, easiest via apt
    python3-libcamera       # libcamera Python bindings
    mosquitto               # MQTT broker
    mosquitto-clients       # mosquitto_pub / mosquitto_sub (testing)
    ffmpeg                  # MP4 muxing for camera recording
    git                     # useful for updates
    libatlas-base-dev       # numpy BLAS optimisations on Pi
)

info "Installing: ${APT_PACKAGES[*]}"
apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}" \
    2>&1 | grep -E "^(Get:|Setting up|Unpacking)" || true
ok "System packages installed"

# ── Step 2: Copy application code ───────────────────────────────────────────
section "Application code"

if [[ "$SCRIPT_DIR" == "$INSTALL_DIR" ]]; then
    info "Source and target are the same directory — no copy needed"
else
    info "Copying code to $INSTALL_DIR…"
    mkdir -p "$INSTALL_DIR"

    # rsync if available (preserves permissions better), otherwise cp
    if command -v rsync &>/dev/null; then
        rsync -a --exclude='.git/' --exclude='__pycache__/' \
              --exclude='.venv/' --exclude='recordings/*.csv' \
              --exclude='recordings/video/*.mp4' \
              "$SCRIPT_DIR/" "$INSTALL_DIR/"
    else
        cp -r "$SCRIPT_DIR/." "$INSTALL_DIR/"
    fi
    ok "Code copied"
fi

# Ensure recordings directories exist with placeholder files
mkdir -p "$INSTALL_DIR/recordings/video"
touch "$INSTALL_DIR/recordings/.gitkeep"
touch "$INSTALL_DIR/recordings/video/.gitkeep"

# Create default state files if they don't already exist
if [[ ! -f "$INSTALL_DIR/silo_state.json" ]]; then
    cat > "$INSTALL_DIR/silo_state.json" <<'JSON'
{
  "relay_open": false,
  "travel_time": 10.0
}
JSON
    info "Created default silo_state.json (travel_time=10 s)"
fi

if [[ ! -f "$INSTALL_DIR/calibration.json" ]]; then
    cat > "$INSTALL_DIR/calibration.json" <<'JSON'
{
  "pitch_offset": 0.0,
  "roll_offset": 0.0
}
JSON
    info "Created default calibration.json"
fi

# Fix ownership
chown -R "$PI_USER:$PI_USER" "$INSTALL_DIR"
ok "Directory ownership set to $PI_USER"

# ── Step 3: Python virtual environment ──────────────────────────────────────
section "Python virtual environment"

if [[ ! -d "$VENV" ]]; then
    info "Creating venv with system site-packages (for cv2/picamera2)…"
    sudo -u "$PI_USER" python3 -m venv --system-site-packages "$VENV"
    ok "Venv created at $VENV"
else
    info "Venv already exists at $VENV"
fi

info "Upgrading pip…"
sudo -u "$PI_USER" "$VENV/bin/pip" install --quiet --upgrade pip

info "Installing pip requirements…"
sudo -u "$PI_USER" "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# python-prctl is needed by picamera2's FfmpegOutput (our fix bypasses it,
# but install anyway so picamera2 internals don't fail at import)
sudo -u "$PI_USER" "$VENV/bin/pip" install --quiet python-prctl || \
    warn "python-prctl install failed (non-fatal — camera will still work)"

ok "Python packages installed"

# Verify critical imports
info "Verifying Python imports…"
FAILED_IMPORTS=()
for mod in flask paho.mqtt.client gpiozero pymavlink; do
    if ! sudo -u "$PI_USER" "$VENV/bin/python" -c "import $mod" 2>/dev/null; then
        FAILED_IMPORTS+=("$mod")
    fi
done
if [[ ${#FAILED_IMPORTS[@]} -gt 0 ]]; then
    warn "Some imports unavailable: ${FAILED_IMPORTS[*]}"
    warn "The application will run in degraded mode for those subsystems"
else
    ok "All core imports verified"
fi

# ── Step 4: mosquitto configuration ─────────────────────────────────────────
section "Mosquitto MQTT broker"

# Write a minimal config that allows anonymous local connections
MOSQ_CONF="/etc/mosquitto/conf.d/silo.conf"
if [[ ! -f "$MOSQ_CONF" ]]; then
    cat > "$MOSQ_CONF" <<'MOSQ'
# Silo Controller — allow anonymous connections on localhost
listener 1883 localhost
allow_anonymous true
MOSQ
    info "Written $MOSQ_CONF"
fi

systemctl enable mosquitto
systemctl restart mosquitto
ok "Mosquitto enabled and running"

# ── Step 5: systemd service ──────────────────────────────────────────────────
section "systemd service"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Patrick Blackett Silo 2 Controller
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
Type=simple
User=pi
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV}/bin/python main.py
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
ok "Service unit written and enabled: $SERVICE_FILE"

# ── Step 6: GPIO permissions ─────────────────────────────────────────────────
section "GPIO permissions"

# Add pi to gpio group so gpiozero can access hardware without root
if getent group gpio &>/dev/null; then
    usermod -aG gpio "$PI_USER"
    ok "Added $PI_USER to gpio group"
else
    warn "gpio group not found — GPIO may need root; recheck after reboot"
fi

# ── Step 7: Start services ───────────────────────────────────────────────────
section "Starting services"

systemctl start "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "silocontroller service is running"
else
    warn "silocontroller service did not start immediately"
    warn "Check logs with:  journalctl -u $SERVICE_NAME -n 50"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${BOLD}${GREEN}Installation complete.${NC}"
echo ""
echo "  Web UI    :  http://${IP}:5000"
echo "  API       :  http://${IP}:5000/api/status"
echo "  Logs      :  journalctl -u $SERVICE_NAME -f"
echo "  Restart   :  sudo systemctl restart $SERVICE_NAME"
echo "  Status    :  sudo systemctl status $SERVICE_NAME"
echo ""
echo -e "${YELLOW}Note:${NC} If the camera or GPIO are not detected, reboot the Pi."
echo "  sudo reboot"
echo ""
