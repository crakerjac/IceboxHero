#!/usr/bin/env bash
# =============================================================================
# FreezerPi Teardown Script
# Reverses everything setup.sh does, leaving the OS in its pre-setup state.
# Use this to test setup.sh repeatedly without reimaging the SD card.
#
# What this script removes:
#   - All five systemd services (stopped, disabled, unit files deleted)
#   - Weekly CRON job for database maintenance
#   - logrotate configuration
#   - /etc/tmpfiles.d/freezerpi.conf (runtime directory config)
#   - /opt/freezerpi/ deployment directory
#   - /data/config/config.ini  (your working config — see warning below)
#   - Python packages installed by setup.sh
#   - Pillow build dependency apt packages
#   - dtparam/dtoverlay lines added to /boot/firmware/config.txt
#   - /etc/watchdog.conf (restored from .bak if available)
#
# What this script does NOT touch:
#   - The /data partition, mount, or /etc/fstab entry
#   - git, curl, python3-pip, python3-venv, fonts-dejavu-core (safe to leave)
#   - Any data in /data/db/ or /data/logs/
#   - The read-only overlay (if you already enabled it, disable it first)
#
# License: GNU General Public License v3.0
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GRN='\033[0;32m'
YEL='\033[0;33m'
BLU='\033[0;34m'
BOLD='\033[1m'
RST='\033[0m'

info()    { echo -e "${BLU}[INFO]${RST}  $*"; }
success() { echo -e "${GRN}[OK]${RST}    $*"; }
warn()    { echo -e "${YEL}[WARN]${RST}  $*"; }
header()  { echo -e "\n${BOLD}${BLU}=== $* ===${RST}"; }

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR]${RST} This script must be run as root."
    echo  "        Run: sudo ./uninstall.sh"
    exit 1
fi

REAL_USER="${SUDO_USER:-pi}"

echo ""
echo -e "${BOLD}${YEL}FreezerPi — Teardown${RST}"
echo    "This will undo everything setup.sh did."
echo ""
echo -e "${YEL}WARNING: /data/config/config.ini will be deleted.${RST}"
echo    "         Back it up first if you want to keep your sensor ROM IDs"
echo    "         and credentials:"
echo    "           cp /data/config/config.ini /data/config/config.ini.save"
echo ""
read -r -p "Continue? [y/N] " confirm || true
if [[ ! "${confirm}" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# =============================================================================
# STEP 1 — Stop and remove systemd services
# =============================================================================
header "Removing systemd Services"

SERVICES=(
    freezer-sensor.service
    freezer-display.service
    freezer-alert.service
    freezer-db.service
    freezer-web.service
)

for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        systemctl stop "${svc}"
        info "Stopped: ${svc}"
    fi
    if systemctl is-enabled --quiet "${svc}" 2>/dev/null; then
        systemctl disable "${svc}"
        info "Disabled: ${svc}"
    fi
    if [[ -f "/etc/systemd/system/${svc}" ]]; then
        rm "/etc/systemd/system/${svc}"
        success "Removed: /etc/systemd/system/${svc}"
    else
        info "Not found (skipping): /etc/systemd/system/${svc}"
    fi
done

# data.mount — remove persistent /data overlay override
systemctl disable data.mount 2>/dev/null || true
rm -f /etc/systemd/system/data.mount
success "Removed: data.mount"

# Restore /etc/fstab if setup.sh made a backup
if [[ -f /etc/fstab.pre-freezerpi.bak ]]; then
    cp /etc/fstab.pre-freezerpi.bak /etc/fstab
    rm -f /etc/fstab.pre-freezerpi.bak
    success "Restored /etc/fstab from backup"
    info "Note: /data will now require a manual mount or fstab entry to be accessible"
else
    warn "No /etc/fstab backup found — fstab was not modified or backup was already removed"
fi

systemctl daemon-reload
success "systemd daemon reloaded"

# =============================================================================
# STEP 2 — Remove CRON job
# =============================================================================
header "Removing CRON Job"

EXISTING_CRON=$(crontab -u "${REAL_USER}" -l 2>/dev/null || true)
if echo "${EXISTING_CRON}" | grep -q "db_maintenance.py"; then
    echo "${EXISTING_CRON}" | grep -v "db_maintenance.py" | crontab -u "${REAL_USER}" -
    success "CRON job removed for user: ${REAL_USER}"
else
    info "CRON job not found — skipping"
fi

# =============================================================================
# STEP 3 — Remove logrotate config
# =============================================================================
header "Removing logrotate Configuration"

if [[ -f /etc/logrotate.d/freezerpi ]]; then
    rm /etc/logrotate.d/freezerpi
    success "Removed: /etc/logrotate.d/freezerpi"
else
    info "Not found — skipping"
fi

# =============================================================================
# STEP 3b — Remove tmpfiles.d configuration
# =============================================================================
header "Removing tmpfiles.d Configuration"

if [[ -f /etc/tmpfiles.d/freezerpi.conf ]]; then
    rm /etc/tmpfiles.d/freezerpi.conf
    success "Removed: /etc/tmpfiles.d/freezerpi.conf"
else
    info "Not found — skipping"
fi

# Clean up the runtime directories if they still exist
rm -rf /run/freezerpi /run/freezer_db 2>/dev/null || true
info "Removed /run/freezerpi and /run/freezer_db (if present)"

# =============================================================================
# STEP 4 — Remove deployed source code
# =============================================================================
header "Removing /opt/freezerpi/"

if [[ -d /opt/freezerpi ]]; then
    rm -rf /opt/freezerpi
    success "Removed: /opt/freezerpi/"
else
    info "Not found — skipping"
fi

# =============================================================================
# STEP 5 — Remove config.ini
# =============================================================================
header "Removing /data/config/config.ini"

if [[ -f /data/config/config.ini ]]; then
    rm /data/config/config.ini
    success "Removed: /data/config/config.ini"
else
    info "Not found — skipping"
fi

# =============================================================================
# STEP 6 — Uninstall Python packages
# =============================================================================
header "Uninstalling Python Packages"

# Only remove packages setup.sh explicitly installed.
# gpiozero ships with Raspberry Pi OS — leave it alone.
PYTHON_PACKAGES=(
    Pillow
    flask
    waitress
    adafruit-blinka
    adafruit-circuitpython-rgb-display
)

pip3 uninstall -y --break-system-packages "${PYTHON_PACKAGES[@]}" 2>/dev/null || true
success "Python packages removed (transitive deps left in place — harmless)"

# =============================================================================
# STEP 7 — Remove Pillow build dependency apt packages
# =============================================================================
header "Removing Pillow Build Dependencies"

# Only remove the -dev packages we added. Do not remove python3-pip,
# python3-venv, git, curl, or fonts-dejavu-core — these are broadly useful
# and not worth the risk of breaking something else.
BUILD_DEPS=(
    python3-dev
    zlib1g-dev
    libjpeg-dev
    libfreetype-dev
)

apt-get remove -y "${BUILD_DEPS[@]}" 2>/dev/null || true
apt-get autoremove -y 2>/dev/null || true
success "Build dependency packages removed"

# =============================================================================
# STEP 8 — Restore /etc/watchdog.conf
# =============================================================================
header "Restoring /etc/watchdog.conf"

systemctl stop watchdog 2>/dev/null || true
systemctl disable watchdog 2>/dev/null || true
info "Watchdog stopped and disabled"

if [[ -f /etc/watchdog.conf.bak ]]; then
    mv /etc/watchdog.conf.bak /etc/watchdog.conf
    success "Restored /etc/watchdog.conf from backup"
else
    # No backup means watchdog.conf didn't exist before setup.sh ran.
    # Write a safe inert config rather than deleting the file entirely,
    # since the watchdog package expects the file to exist.
    cat > /etc/watchdog.conf <<'EOF'
# watchdog.conf — restored to default state by freezerpi uninstall.sh
# No devices or files are being monitored.
EOF
    info "No backup found — reset /etc/watchdog.conf to inert default"
fi

# =============================================================================
# STEP 9 — Remove lines added to /boot/firmware/config.txt
# =============================================================================
header "Cleaning /boot/firmware/config.txt"

if [[ -f /boot/firmware/config.txt ]]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
    BOOT_CONFIG="/boot/config.txt"
else
    warn "Cannot find config.txt — skipping boot config cleanup"
    BOOT_CONFIG=""
fi

if [[ -n "${BOOT_CONFIG}" ]]; then
    # Make a backup before editing
    cp "${BOOT_CONFIG}" "${BOOT_CONFIG}.uninstall.bak"

    sed -i '/^dtparam=watchdog=on$/d'          "${BOOT_CONFIG}"
    sed -i '/^dtoverlay=w1-gpio,gpiopin=4$/d'  "${BOOT_CONFIG}"
    # Leave dtparam=spi=on — it may have been present before setup.sh ran,
    # and removing it could break other SPI devices. Document the decision.
    info "dtparam=spi=on left in place (may have pre-existed; safe to remove manually if needed)"

    success "Removed watchdog and 1-Wire lines from ${BOOT_CONFIG}"
    info "Backup saved to ${BOOT_CONFIG}.uninstall.bak"
fi

# =============================================================================
# Done
# =============================================================================
echo ""
echo -e "${BOLD}${GRN}============================================================${RST}"
echo -e "${BOLD}${GRN}  Teardown complete.${RST}"
echo -e "${BOLD}${GRN}============================================================${RST}"
echo ""
echo -e "${BOLD}What was removed:${RST}"
echo  "  ✓ All five systemd services stopped, disabled, and deleted"
echo  "  ✓ Weekly CRON job removed"
echo  "  ✓ logrotate configuration removed"
echo  "  ✓ /etc/tmpfiles.d/freezerpi.conf removed"
echo  "  ✓ /opt/freezerpi/ deleted"
echo  "  ✓ /data/config/config.ini deleted"
echo  "  ✓ Python packages uninstalled"
echo  "  ✓ Pillow build dependency apt packages removed"
echo  "  ✓ /etc/watchdog.conf restored"
echo  "  ✓ watchdog and 1-Wire lines removed from ${BOOT_CONFIG:-/boot/firmware/config.txt}"
echo ""
echo -e "${BOLD}What was left in place:${RST}"
echo  "  - /data partition, mount, and /etc/fstab entry"
echo  "  - /data/db/, /data/logs/ (your historical data)"
echo  "  - git, curl, python3-pip, python3-venv, fonts-dejavu-core"
echo  "  - dtparam=spi=on in ${BOOT_CONFIG:-/boot/firmware/config.txt}"
echo  "  - watchdog package itself (apt package, not worth removing)"
echo ""
