#!/usr/bin/env bash
# =============================================================================
# FreezerPi — Start Services
# Starts all FreezerPi services and the hardware watchdog.
#
# Usage:
#   sudo ./start_services.sh
#
# NOTE: Do not run this until:
#   - /data/config/config.ini has been edited with real sensor ROM IDs
#   - DS18B20 sensors are physically connected
#   - The Pi has been rebooted at least once after setup.sh (for hardware
#     overlays to load)
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
error()   { echo -e "${RED}[ERROR]${RST} $*"; }
header()  { echo -e "\n${BOLD}${BLU}=== $* ===${RST}"; }

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root."
    echo  "      Run: sudo ./start_services.sh"
    exit 1
fi

SERVICES=(
    freezer-sensor.service
    freezer-display.service
    freezer-alert.service
    freezer-db.service
    freezer-web.service
)

echo ""
echo -e "${BOLD}FreezerPi — Starting Services${RST}"
echo ""

# =============================================================================
# Preflight: confirm sensors are present on the 1-Wire bus
# =============================================================================
header "Checking for DS18B20 Sensors"

SENSOR_COUNT=$(ls -d /sys/bus/w1/devices/28-*/ 2>/dev/null | wc -l || true)
if [[ "${SENSOR_COUNT}" -eq 0 ]]; then
    warn "No DS18B20 sensors detected at /sys/bus/w1/devices/28-*"
    warn "Starting services anyway, but sensor_service will report missing sensors."
    warn "The watchdog will reboot the Pi in 180 seconds if the IPC file is never written."
    echo ""
    read -r -p "Continue anyway? [y/N] " confirm || true
    if [[ ! "${confirm}" =~ ^[Yy]$ ]]; then
        echo "Aborted. Connect sensors and reboot, then re-run this script."
        exit 0
    fi
else
    success "Found ${SENSOR_COUNT} sensor(s) on the 1-Wire bus"
    ls -d /sys/bus/w1/devices/28-*/ 2>/dev/null | while read -r s; do
        info "  $(basename "${s}")"
    done
fi

# =============================================================================
# Confirm config.ini has been edited
# =============================================================================
header "Checking Configuration"

CONFIG_FILE="/data/config/config.ini"
if [[ ! -f "${CONFIG_FILE}" ]]; then
    error "${CONFIG_FILE} not found. Run setup.sh first."
    exit 1
fi

if grep -q "28-00000xxxxxxx" "${CONFIG_FILE}"; then
    warn "config.ini still contains placeholder sensor ROM IDs."
    warn "Edit ${CONFIG_FILE} before starting — services will not work correctly."
    echo ""
    read -r -p "Continue anyway? [y/N] " confirm || true
    if [[ ! "${confirm}" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
else
    success "config.ini looks configured"
fi

# =============================================================================
# Start FreezerPi services
# =============================================================================
header "Starting FreezerPi Services"

for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        info "Already running: ${svc}"
    else
        systemctl start "${svc}"
        success "Started: ${svc}"
    fi
done

# =============================================================================
# Start watchdog last — only after services are up and writing the IPC file
# =============================================================================
header "Starting Hardware Watchdog"

WATCHDOG_STATE_FILE="/data/watchdog_was_active"
if [[ -f "${WATCHDOG_STATE_FILE}" ]]; then
    # Watchdog was active when stop_services.sh was last run — re-arm it
    info "Watchdog was previously active. Waiting 5 seconds for sensor_service to initialize..."
    sleep 5
    systemctl start watchdog
    rm -f "${WATCHDOG_STATE_FILE}"
    success "Watchdog re-armed"
else
    # Watchdog was not running before — don't arm it automatically.
    # Use start_services.sh --watchdog to arm it explicitly if needed.
    info "Watchdog was not previously active — leaving it disabled"
    info "To arm it manually: sudo systemctl start watchdog"
fi

# =============================================================================
# Status summary
# =============================================================================
echo ""
echo -e "${BOLD}${GRN}============================================================${RST}"
echo -e "${BOLD}${GRN}  All services started.${RST}"
echo -e "${BOLD}${GRN}============================================================${RST}"
echo ""

for svc in "${SERVICES[@]}" watchdog.service; do
    STATUS=$(systemctl is-active "${svc}" 2>/dev/null || true)
    # Normalize — 'unknown' means never started this boot, treat same as inactive
    [[ "${STATUS}" == "unknown" || -z "${STATUS}" ]] && STATUS="inactive"
    if [[ "${STATUS}" == "active" ]]; then
        echo -e "  ${GRN}●${RST} ${svc}"
    else
        echo -e "  ${RED}●${RST} ${svc} (${STATUS})"
    fi
done

echo ""
echo -e "${BOLD}Useful diagnostics:${RST}"
echo  "  journalctl -u freezer-sensor.service -f"
echo  "  cat /run/freezerpi/telemetry_state.json"
echo  "  systemctl status 'freezer-*'"
echo ""
