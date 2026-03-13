#!/usr/bin/env bash
# =============================================================================
# IceboxHero — Stop Services
#
# Usage:
#   sudo ./stop_services.sh
#
# Stops icebox-watchdog first so the hardware watchdog is disarmed before
# IceboxHero services stop writing the IPC file. Then stops all services.
# On next boot, icebox-watchdog.service re-arms automatically.
#
# Use this script before:
#   - Editing and redeploying source files
#   - Running uninstall.sh
#   - Any maintenance that would interrupt IPC file updates
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
header()  { echo -e "\n${BOLD}${BLU}=== $* ===${RST}"; }

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR]${RST} This script must be run as root."
    echo  "        Run: sudo ./stop_services.sh"
    exit 1
fi

SERVICES=(
    icebox-watchdog.service
    icebox-sensor.service
    icebox-display.service
    icebox-alert.service
    icebox-db.service
    icebox-web.service
)

echo ""
echo -e "${BOLD}IceboxHero — Stopping Services${RST}"
echo ""

# =============================================================================
# Stop all services (watchdog first in the SERVICES array above)
# =============================================================================
header "Stopping IceboxHero Services"

for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        systemctl stop "${svc}"
        success "Stopped: ${svc}"
    else
        info "Not running: ${svc}"
    fi
done

# =============================================================================
# Status summary
# =============================================================================
echo ""
echo -e "${BOLD}${YEL}============================================================${RST}"
echo -e "${BOLD}${YEL}  All services stopped.${RST}"
echo -e "${BOLD}${YEL}============================================================${RST}"
echo ""

for svc in "${SERVICES[@]}"; do
    STATUS=$(systemctl is-active "${svc}" 2>/dev/null || echo "inactive")
    if [[ "${STATUS}" == "inactive" || "${STATUS}" == "dead" || "${STATUS}" == "unknown" ]]; then
        echo -e "  ${GRN}●${RST} ${svc} (stopped)"
    else
        echo -e "  ${YEL}●${RST} ${svc} (${STATUS})"
    fi
done

echo ""
echo -e "${BOLD}Note:${RST} Services are stopped but still enabled."
echo  "  They will restart automatically on next reboot."
echo  "  To start them again now: sudo ./start_services.sh"
echo ""
