#!/usr/bin/env bash
# =============================================================================
# IceboxHero — Start Services
#
# Usage:
#   sudo ./start_services.sh
#
# NOTE: On a normal boot all services start automatically — you only need
# this script after manually stopping services for maintenance.
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
    icebox-sensor.service
    icebox-display.service
    icebox-alert.service
    icebox-db.service
    icebox-web.service
    icebox-watchdog.service
)

echo ""
echo -e "${BOLD}IceboxHero — Starting Services${RST}"
echo ""

# =============================================================================
# Preflight: confirm sensors are present on the 1-Wire bus
# =============================================================================
header "Checking for DS18B20 Sensors"

SENSOR_COUNT=$(ls -d /sys/bus/w1/devices/28-*/ 2>/dev/null | wc -l || true)
if [[ "${SENSOR_COUNT}" -eq 0 ]]; then
    warn "No DS18B20 sensors detected at /sys/bus/w1/devices/28-*"
    warn "sensor_service will report missing sensors."
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
# Start services
# =============================================================================
header "Starting IceboxHero Services"

for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        info "Already running: ${svc}"
    else
        systemctl start "${svc}"
        success "Started: ${svc}"
    fi
done

# =============================================================================
# Status summary
# =============================================================================
echo ""
echo -e "${BOLD}${GRN}============================================================${RST}"
echo -e "${BOLD}${GRN}  All services started.${RST}"
echo -e "${BOLD}${GRN}============================================================${RST}"
echo ""

for svc in "${SERVICES[@]}"; do
    STATUS=$(systemctl is-active "${svc}" 2>/dev/null || true)
    [[ "${STATUS}" == "unknown" || -z "${STATUS}" ]] && STATUS="inactive"
    if [[ "${STATUS}" == "active" ]]; then
        echo -e "  ${GRN}●${RST} ${svc}"
    else
        echo -e "  ${RED}●${RST} ${svc} (${STATUS})"
    fi
done

echo ""
echo -e "${BOLD}Useful diagnostics:${RST}"
echo  "  journalctl -u icebox-sensor.service -f"
echo  "  cat /run/iceboxhero/telemetry_state.json"
echo  "  systemctl status 'icebox-*'"
echo ""
