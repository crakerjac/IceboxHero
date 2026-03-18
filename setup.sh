#!/usr/bin/env bash
# =============================================================================
# IceboxHero Setup Script
# https://github.com/crakerjac/IceboxHero
#
# Usage (from cloned repo):
#   sudo ./setup.sh
#
# Usage (downloaded standalone — no git required on the Pi beforehand):
#   curl -fsSL https://raw.githubusercontent.com/crakerjac/IceboxHero/main/setup.sh -o setup.sh
#   sudo bash setup.sh
#   (The script will install git and clone the repo automatically.)
#
# What this script does (automatically):
#   - Installs system packages (watchdog, python3-pip, etc.)
#   - Installs Python dependencies
#   - Deploys source code to /opt/iceboxhero/
#   - Copies config template to /data/config/config.ini (if not present)
#   - Downloads Chart.js for the local web dashboard
#   - Configures /boot/firmware/config.txt (watchdog, SPI, 1-Wire)
#   - Installs tmpfiles.d config (/run/iceboxhero and /run/icebox_db owned by pi)
#   - Configures /etc/watchdog.conf
#   - Installs logrotate configuration
#   - Installs and enables all five systemd services
#   - Adds the weekly database maintenance CRON job
#
# What you must do manually (before running this script):
#   - Create and mount the /data ext4 partition (see README Step 1)
#   - Add /dev/mmcblk0p3 to /etc/fstab is NOT required — data.mount handles it
#
# What you must do manually (after running this script):
#   - Edit /data/config/config.ini with your sensor ROM IDs and credentials
#   - Download and place chart.min.js if the automatic download fails
#   - Enable the read-only overlay LAST, after verifying everything works:
#       sudo raspi-config → Performance Options → Overlay File System
#
# License: GNU General Public License v3.0
# =============================================================================

set -euo pipefail

# =============================================================================
# Colour helpers
# =============================================================================
RED='\033[0;31m'
GRN='\033[0;32m'
YEL='\033[0;33m'
BLU='\033[0;34m'
BOLD='\033[1m'
RST='\033[0m'

info()    { echo -e "${BLU}[INFO]${RST}  $*"; }
success() { echo -e "${GRN}[OK]${RST}    $*"; }
warn()    { echo -e "${YEL}[WARN]${RST}  $*"; }
error()   { echo -e "${RED}[ERROR]${RST} $*"; }
header()  { echo -e "\n${BOLD}${BLU}=== $* ===${RST}"; }

# =============================================================================
# Must run as root
# =============================================================================
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root."
    echo  "      Run: sudo ./setup.sh"
    exit 1
fi

# Capture the real user who invoked sudo (for crontab installation)
REAL_USER="${SUDO_USER:-pi}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# =============================================================================
# Bootstrap: if run as a standalone downloaded script (not from a cloned repo),
# install git, clone the repo, and re-exec from the correct location.
# This supports: curl -fsSL .../setup.sh | sudo bash
# =============================================================================
REPO_URL="https://github.com/crakerjac/IceboxHero.git"

if [[ ! -f "${SCRIPT_DIR}/config.ini.template" ]]; then
    echo ""
    echo -e "${BOLD}Bootstrap mode:${RST} repository files not found alongside this script."
    echo -e "Installing git and cloning ${REPO_URL}...\n"

    apt-get update -qq
    apt-get install -y --no-install-recommends git

    CLONE_DIR="/opt/iceboxhero-src"
    if [[ -d "${CLONE_DIR}/.git" ]]; then
        info "Updating existing clone at ${CLONE_DIR}..."
        git -C "${CLONE_DIR}" pull --ff-only
    else
        git clone "${REPO_URL}" "${CLONE_DIR}"
    fi

    chown -R "${REAL_USER}:${REAL_USER}" "${CLONE_DIR}"
    echo ""
    echo -e "Re-executing setup from repository root...\n"
    exec bash "${CLONE_DIR}/setup.sh"
fi

echo ""
echo -e "${BOLD}IceboxHero — Automated Setup${RST}"
echo    "Running from: ${SCRIPT_DIR}"
echo    "Installing as user: ${REAL_USER}"
echo ""

# =============================================================================
# STEP 0 — Preflight checks
# =============================================================================
header "Preflight Checks"

# Confirm /data is mounted
if ! mountpoint -q /data; then
    error "/data is not mounted."
    echo ""
    echo  "  The /data partition must be created and mounted before running this"
    echo  "  script. See README Step 1 for the one-time partition setup:"
    echo ""
    echo  "    sudo fdisk /dev/mmcblk0        # create p3"
    echo  "    sudo mkfs.ext4 /dev/mmcblk0p3"
    echo  "    sudo mkdir -p /data/config /data/db /data/logs"
    echo  "    sudo mount /dev/mmcblk0p3 /data"
    echo  "    sudo chown -R ${REAL_USER}:${REAL_USER} /data"
    echo  ""
    echo  "  Do NOT add /data to /etc/fstab — setup.sh installs a systemd"
    echo  "  data.mount unit that handles mounting and bypasses overlayroot."
    echo ""
    exit 1
fi
success "/data is mounted"

# Check we're on a Raspberry Pi
if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null && \
   ! grep -q "Raspberry Pi" /sys/firmware/devicetree/base/model 2>/dev/null; then
    warn "Could not confirm this is a Raspberry Pi. Proceeding anyway."
else
    MODEL=$(cat /sys/firmware/devicetree/base/model 2>/dev/null | tr -d '\0' || echo "Raspberry Pi")
    success "Detected: ${MODEL}"
fi

# Confirm script is running from the repo root (sanity check)
if [[ ! -f "${SCRIPT_DIR}/config.ini.template" ]]; then
    error "config.ini.template not found in ${SCRIPT_DIR}"
    echo  "      Run this script from the root of the cloned repository."
    exit 1
fi
success "Repository structure looks correct"

# =============================================================================
# STEP 1 — /boot/firmware/config.txt
# =============================================================================
header "Configuring /boot/firmware/config.txt"

# Support both newer and older Pi OS boot paths
if [[ -f /boot/firmware/config.txt ]]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
    BOOT_CONFIG="/boot/config.txt"
    warn "Using legacy boot path: /boot/config.txt"
else
    error "Cannot find config.txt in /boot/firmware/ or /boot/"
    exit 1
fi

add_boot_param() {
    local param="$1"
    if grep -qF "^${param}" "${BOOT_CONFIG}"; then
        info "Already set: ${param}"
    elif grep -qF "#${param}" "${BOOT_CONFIG}"; then
        # Uncomment existing commented-out line
        sed -i "s|^#${param}|${param}|" "${BOOT_CONFIG}"
        success "Uncommented: ${param}"
    else
        echo "${param}" >> "${BOOT_CONFIG}"
        success "Added: ${param}"
    fi
}

add_boot_param "dtparam=watchdog=on"
add_boot_param "dtparam=spi=on"
add_boot_param "dtoverlay=w1-gpio,gpiopin=4"

warn "A reboot is required after setup for these hardware changes to take effect."

# =============================================================================
# STEP 2 — System packages
# =============================================================================
header "Installing System Packages"

apt-get update -qq
# Core tools + Pillow C build dependencies.
# python3-dev    : Python headers (Python.h) required to compile any C extension
# zlib1g-dev     : PNG support in Pillow
# libjpeg-dev    : JPEG support in Pillow
# libfreetype6-dev: TrueType font rendering (required by display_service.py)
# Pre-built Pillow wheels are not yet available for Python 3.13 on 32-bit ARM,
# so pip compiles from source — all three -dev headers are required.
apt-get install -y --no-install-recommends \
    git \
    watchdog \
    python3-pip \
    python3-dev \
    python3-venv \
    fonts-dejavu-core \
    curl \
    zlib1g-dev \
    libjpeg-dev \
    libfreetype6-dev
success "System packages installed"

# =============================================================================
# STEP 3 — Python dependencies
# =============================================================================
header "Installing Python Dependencies"

pip3 install --break-system-packages --retries 5 --root-user-action=ignore \
    gpiozero \
    Pillow \
    flask \
    waitress \
    adafruit-blinka \
    adafruit-circuitpython-rgb-display \
    adafruit-circuitpython-st7735r
success "Python dependencies installed"

# =============================================================================
# STEP 4 — Deploy source code to /opt/iceboxhero/
# =============================================================================
header "Deploying Source Code to /opt/iceboxhero/"

install -d -m 755 -o "${REAL_USER}" -g "${REAL_USER}" \
    /opt/iceboxhero \
    /opt/iceboxhero/templates \
    /opt/iceboxhero/static

# Watchdog repair script
install -m 755 -o root -g root "${SCRIPT_DIR}/watchdog_repair.sh" /opt/iceboxhero/watchdog_repair.sh
success "Deployed: watchdog_repair.sh"

# Python modules
for f in config_helper.py sensor_service.py display_service.py \
          alert_service.py db_logger.py db_maintenance.py web_server.py mock_sensors.py display_test.py; do
    if [[ -f "${SCRIPT_DIR}/${f}" ]]; then
        install -m 644 -o "${REAL_USER}" -g "${REAL_USER}" \
            "${SCRIPT_DIR}/${f}" "/opt/iceboxhero/${f}"
        success "Deployed: ${f}"
    else
        error "Missing source file: ${f}"
        exit 1
    fi
done

# HTML template
install -m 644 -o "${REAL_USER}" -g "${REAL_USER}" \
    "${SCRIPT_DIR}/templates/index.html" \
    "/opt/iceboxhero/templates/index.html"
success "Deployed: templates/index.html"

# Static assets
if [[ -f "${SCRIPT_DIR}/static/favicon.png" ]]; then
    install -m 644 -o "${REAL_USER}" -g "${REAL_USER}" \
        "${SCRIPT_DIR}/static/favicon.png" \
        "/opt/iceboxhero/static/favicon.png"
    success "Deployed: static/favicon.png"
fi

install -m 644 -o "${REAL_USER}" -g "${REAL_USER}" \
    "${SCRIPT_DIR}/VERSION" "/opt/iceboxhero/VERSION"
success "Deployed: VERSION"

# =============================================================================
# STEP 5 — /data directory structure
# =============================================================================
header "Setting Up /data Directory Structure"

install -d -m 755 -o "${REAL_USER}" -g "${REAL_USER}" \
    /data/config \
    /data/db \
    /data/logs
success "/data directories created"

# =============================================================================
# STEP 6 — Configuration file
# =============================================================================
header "Configuration File"

if [[ -f /data/config/config.ini ]]; then
    warn "/data/config/config.ini already exists — leaving it untouched."
    info "If you want a fresh config, delete it and re-run this script."
else
    install -m 640 -o "${REAL_USER}" -g "${REAL_USER}" \
        "${SCRIPT_DIR}/config.ini.template" \
        "/data/config/config.ini"
    success "Copied config.ini.template → /data/config/config.ini"
fi

# =============================================================================
# STEP 7 — Chart.js (local hosting, no CDN)
# =============================================================================
header "Downloading Chart.js"

CHARTJS_URL="https://cdn.jsdelivr.net/npm/chart.js/dist/chart.umd.min.js"
CHARTJS_DEST="/opt/iceboxhero/static/chart.min.js"

if [[ -f "${CHARTJS_DEST}" ]]; then
    info "chart.min.js already present — skipping download."
else
    if curl -fsSL --connect-timeout 10 "${CHARTJS_URL}" -o "${CHARTJS_DEST}"; then
        chown "${REAL_USER}:${REAL_USER}" "${CHARTJS_DEST}"
        success "chart.min.js downloaded"
    else
        warn "Download failed. Dashboard graph will not render until you manually place"
        warn "chart.min.js at: ${CHARTJS_DEST}"
        warn "Download from: https://github.com/chartjs/Chart.js/releases"
    fi
fi

# =============================================================================
# STEP 8 — Watchdog daemon
# =============================================================================
header "Configuring Hardware Watchdog"

WATCHDOG_CONF="/etc/watchdog.conf"

# Write only the parameters we need, preserving any existing file as a backup
if [[ -f "${WATCHDOG_CONF}" ]]; then
    cp "${WATCHDOG_CONF}" "${WATCHDOG_CONF}.bak"
    info "Backed up existing watchdog.conf to ${WATCHDOG_CONF}.bak"
fi

cat > "${WATCHDOG_CONF}" <<'EOF'
watchdog-device  = /dev/watchdog
watchdog-timeout = 15
max-load-1       = 24
# Trigger a hardware reboot if the sensor service stops updating the IPC file
file   = /run/iceboxhero/telemetry_state.json
change = 180
# Run repair script before rebooting — sets pending email flag in alert_state.json
repair-binary    = /opt/iceboxhero/watchdog_repair.sh
EOF

# Do NOT enable or start the watchdog here. The watchdog monitors the IPC
# file for updates — if sensors are not connected yet, it will trigger a
# reboot loop. icebox-watchdog.service arms the watchdog only after
# confirming the IPC file exists.
systemctl disable watchdog 2>/dev/null || true
systemctl stop watchdog 2>/dev/null || true

# The Debian watchdog package gates startup on run_watchdog=1 in
# /etc/default/watchdog. Without this the service exits silently at boot
# and systemctl start watchdog hangs waiting for a PID that never arrives.
if [[ -f /etc/default/watchdog ]]; then
    sed -i 's/^run_watchdog=.*/run_watchdog=1/' /etc/default/watchdog
    grep -q '^run_watchdog=' /etc/default/watchdog || echo 'run_watchdog=1' >> /etc/default/watchdog
else
    echo 'run_watchdog=1' > /etc/default/watchdog
fi
success "Watchdog configured (disabled — armed by icebox-watchdog.service)"

# Disable systemd's built-in hardware watchdog. By default systemd claims
# /dev/watchdog0 for itself (RuntimeWatchdogSec=1min) which prevents the
# userspace watchdog daemon from opening /dev/watchdog. Systemd's watchdog
# only checks that systemd is alive — it won't detect a frozen sensor_service.
# Our daemon monitors the IPC file, which is the correct check for IceboxHero.
SYSTEMD_WD_CONF="/etc/systemd/system.conf.d/disable-runtime-watchdog.conf"
mkdir -p /etc/systemd/system.conf.d/
cat > "${SYSTEMD_WD_CONF}" <<'EOF'
[Manager]
RuntimeWatchdogSec=0
ShutdownWatchdogSec=0
EOF
success "Systemd RuntimeWatchdog disabled (${SYSTEMD_WD_CONF})"
info "Reboot required for this to take effect"

# =============================================================================
# STEP 9 — logrotate
# =============================================================================
header "Installing logrotate Configuration"

cat > /etc/logrotate.d/iceboxhero <<'EOF'
/data/logs/db_maintenance.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
EOF

success "logrotate configured"

# =============================================================================
# STEP 10 — tmpfiles.d runtime directories
# =============================================================================
header "Installing tmpfiles.d Configuration"

# IceboxHero services run as the unprivileged 'pi' user but need to write
# to /run, which is owned by root and not world-writable. The systemd-tmpfiles
# mechanism creates these directories at every boot before services start,
# owned by pi, so no service needs to run as root.
cat > /etc/tmpfiles.d/iceboxhero.conf <<'EOF'
# /etc/tmpfiles.d/iceboxhero.conf
# Creates runtime directories and IPC file in /run owned by pi at every boot.
# /run/iceboxhero  — IPC state file, corruption flag
# /run/icebox_db — live SQLite RAM database
#
# The IPC file is pre-created so the watchdog daemon has a file to monitor
# from the moment it starts. sensor_service overwrites it with real data
# within its first poll cycle. Without this, the watchdog would see a
# missing file at boot and trigger an immediate reboot.
d /run/iceboxhero   0755 pi pi -
d /run/icebox_db  0755 pi pi -
f /run/iceboxhero/telemetry_state.json 0644 pi pi - {"sensors":{},"timestamp":0,"boot":true}
EOF

# Apply immediately so services can start without a reboot
systemd-tmpfiles --create /etc/tmpfiles.d/iceboxhero.conf
success "tmpfiles.d configuration installed (/run/iceboxhero and /run/icebox_db created)"

# =============================================================================
# STEP 11 — systemd services
# =============================================================================
header "Installing systemd Services"

# data.mount — makes /data a real persistent partition even with overlayroot enabled.
# The overlayroot initramfs overlays every fstab entry it finds. The fix is to
# remove /data from fstab entirely so initramfs never sees it, then let this
# systemd unit mount it directly after handoff to systemd.
install -m 644 "${SCRIPT_DIR}/systemd/data.mount" "/etc/systemd/system/data.mount"
success "Installed: data.mount"

# Remove /data from /etc/fstab if present — data.mount takes over from here
if grep -q "mmcblk0p3\|[[:space:]]/data[[:space:]]" /etc/fstab; then
    cp /etc/fstab /etc/fstab.pre-iceboxhero.bak
    sed -i '/[[:space:]]\/data[[:space:]]/d' /etc/fstab
    sed -i '/mmcblk0p3/d' /etc/fstab
    success "Removed /data entry from /etc/fstab (backup: /etc/fstab.pre-iceboxhero.bak)"
else
    info "/data not in /etc/fstab — nothing to remove"
fi

SERVICES=(
    icebox-sensor.service
    icebox-display.service
    icebox-alert.service
    icebox-db.service
    icebox-web.service
    icebox-watchdog.service
)

for svc in "${SERVICES[@]}"; do
    src="${SCRIPT_DIR}/systemd/${svc}"
    if [[ -f "${src}" ]]; then
        install -m 644 "${src}" "/etc/systemd/system/${svc}"
        success "Installed: ${svc}"
    else
        error "Missing service file: ${src}"
        exit 1
    fi
done

systemctl daemon-reload

systemctl enable data.mount
success "Enabled: data.mount"

for svc in "${SERVICES[@]}"; do
    systemctl enable "${svc}"
    success "Enabled: ${svc}"
done

info "Services will start automatically on next boot."
info "To start them now (after editing config.ini): sudo ./start_services.sh"

# =============================================================================
# STEP 12 — CRON job for weekly database maintenance
# =============================================================================
header "Installing Weekly Maintenance CRON Job"

CRON_JOB="0 3 * * 0 /usr/bin/python3 /opt/iceboxhero/db_maintenance.py >> /data/logs/db_maintenance.log 2>&1"

# Add only if not already present
EXISTING_CRON=$(crontab -u "${REAL_USER}" -l 2>/dev/null || true)
if echo "${EXISTING_CRON}" | grep -qF "db_maintenance.py"; then
    info "CRON job already present — skipping."
else
    (echo "${EXISTING_CRON}"; echo "${CRON_JOB}") | crontab -u "${REAL_USER}" -
    success "CRON job added for user: ${REAL_USER}"
fi

# =============================================================================
# Done — Summary
# =============================================================================
echo ""
echo -e "${BOLD}${GRN}============================================================${RST}"
echo -e "${BOLD}${GRN}  Setup complete!${RST}"
echo -e "${BOLD}${GRN}============================================================${RST}"
echo ""
echo -e "${BOLD}What was done:${RST}"
echo  "  ✓ Hardware interfaces configured in ${BOOT_CONFIG}"
echo  "  ✓ System packages installed"
echo  "  ✓ Python dependencies installed"
echo  "  ✓ Source code deployed to /opt/iceboxhero/"
echo  "  ✓ /data directory structure created"
echo  "  ✓ Watchdog daemon configured (auto-armed at boot by icebox-watchdog.service)"
echo  "  ✓ tmpfiles.d configured (/run/iceboxhero and /run/icebox_db created, pi-owned)"
echo  "  ✓ logrotate configured"
echo  "  ✓ Five systemd services installed and enabled"
echo  "  ✓ Weekly CRON job scheduled for database maintenance"
echo ""
echo -e "${BOLD}${YEL}Required manual steps before the system will run:${RST}"
echo ""
echo -e "  ${BOLD}1. Edit your config file with real values:${RST}"
echo  "       sudo nano /data/config/config.ini"
echo  "     Required: sensor ROM IDs, Gmail address, Gmail App Password"
echo  "     Optional: healthchecks.io UUIDs (leave as placeholders to disable)"
echo ""
echo -e "  ${BOLD}2. Find your DS18B20 sensor ROM IDs:${RST}"
echo  "     Reboot first (hardware overlay needs to load), then run:"
echo  "       ls /sys/bus/w1/devices/"
echo  "     Look for entries starting with '28-'. Each is one sensor."
echo ""
echo -e "  ${BOLD}3. Reboot to activate hardware changes:${RST}"
echo  "       sudo reboot"
echo ""
echo -e "  ${BOLD}4. Connect sensors, then start all services:${RST}"
echo  "       sudo ./start_services.sh"
echo  "     This confirms sensors are detected, starts all services, and arms the watchdog last."
echo ""
echo -e "  ${BOLD}5. Verify everything is working, then enable the read-only overlay:${RST}"
echo  "       sudo raspi-config → Performance Options → Overlay File System → Enable"
echo  "     Do this LAST. Once enabled, the root filesystem is read-only."
echo ""
echo -e "  ${BOLD}Useful diagnostics:${RST}"
echo  "       journalctl -u icebox-sensor.service -f"
echo  "       cat /run/iceboxhero/telemetry_state.json"
echo  "       systemctl status 'icebox-*'"
echo ""
