#!/usr/bin/env bash
# =============================================================================
# FreezerPi — Update Script
#
# Safely updates FreezerPi with the overlay filesystem enabled or disabled.
# Because the overlay makes root read-only, updates require two reboots:
#
#   Phase 1 — Prepare:  disable overlay, save state, reboot
#   Phase 2 — Apply:    pull latest, run setup, validate, enable overlay, reboot
#   Phase 3 — Verify:   confirm all services healthy after second reboot
#
# Usage:
#   sudo ./update.sh prepare   — Phase 1: disable overlay and reboot
#   sudo ./update.sh apply     — Phase 2: update and re-enable overlay (run after reboot)
#   sudo ./update.sh verify    — Phase 3: confirm services healthy (run after second reboot)
#   sudo ./update.sh status    — Show current update state and overlay status
#
# If the overlay is NOT enabled, you can skip prepare/verify and just run:
#   sudo ./update.sh apply
#
# State is persisted at /data/update_state so it survives reboots.
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
    error "This script must be run as root. Use: sudo ./update.sh <phase>"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="/data/update_state"
SERVICES=(freezer-sensor freezer-display freezer-alert freezer-db freezer-web)

# =============================================================================
# Helpers
# =============================================================================

overlay_is_enabled() {
    # Overlay is active if /proc/mounts shows overlayroot on /
    grep -q "overlayroot\|overlay" /proc/mounts 2>/dev/null && return 0
    # Also check raspi-config's record
    raspi-config nonint get_overlayfs 2>/dev/null | grep -q "^1$" && return 0
    return 1
}

write_state() {
    cat > "${STATE_FILE}" <<EOF
phase=$1
timestamp=$(date -Iseconds)
version_from=$(cat "${SCRIPT_DIR}/VERSION" 2>/dev/null || echo "unknown")
EOF
    info "State saved: phase=$1"
}

read_state() {
    [[ -f "${STATE_FILE}" ]] && cat "${STATE_FILE}" || echo "phase=none"
}

current_phase() {
    [[ -f "${STATE_FILE}" ]] && grep "^phase=" "${STATE_FILE}" | cut -d= -f2 || echo "none"
}

validate_services() {
    local all_ok=true
    header "Validating Services"

    for svc in "${SERVICES[@]}"; do
        STATUS=$(systemctl is-active "${svc}.service" 2>/dev/null || echo "inactive")
        if [[ "${STATUS}" == "active" ]]; then
            success "${svc}: active"
        else
            error "${svc}: ${STATUS}"
            all_ok=false
        fi
    done

    header "Validating IPC File"
    if [[ -f /run/freezerpi/telemetry_state.json ]]; then
        AGE=$(( $(date +%s) - $(stat -c %Y /run/freezerpi/telemetry_state.json) ))
        if [[ ${AGE} -lt 600 ]]; then
            success "telemetry_state.json exists (${AGE}s old)"
        else
            warn "telemetry_state.json is stale (${AGE}s old)"
        fi
    else
        error "telemetry_state.json not found"
        all_ok=false
    fi

    header "Validating Web Server"
    WEB_PORT=$(grep "web_port" /data/config/config.ini 2>/dev/null | cut -d= -f2 | tr -d ' ' || echo "8080")
    if curl -sf "http://localhost:${WEB_PORT}/api/current" > /dev/null 2>&1; then
        success "Web server responding on port ${WEB_PORT}"
    else
        error "Web server not responding on port ${WEB_PORT}"
        all_ok=false
    fi

    header "Validating Config"
    if [[ -f /data/config/config.ini ]]; then
        if grep -q "28-00000xxxxxxx" /data/config/config.ini; then
            warn "config.ini still contains placeholder ROM IDs"
        else
            success "config.ini present and configured"
        fi
    else
        error "config.ini not found"
        all_ok=false
    fi

    header "Installed Version"
    VERSION=$(cat /opt/freezerpi/VERSION 2>/dev/null || echo "unknown")
    success "Version: ${VERSION}"

    if [[ "${all_ok}" == "true" ]]; then
        return 0
    else
        return 1
    fi
}

# =============================================================================
# Phase 1 — Prepare: disable overlay and reboot
# =============================================================================

phase_prepare() {
    header "Update Prepare — Phase 1 of 3"

    if ! overlay_is_enabled; then
        warn "Overlay filesystem does not appear to be enabled."
        info "You can skip directly to: sudo ./update.sh apply"
        echo ""
        read -r -p "Run apply phase now instead? [y/N] " confirm
        if [[ "${confirm}" =~ ^[Yy]$ ]]; then
            phase_apply
            return
        else
            echo "Aborted."
            exit 0
        fi
    fi

    echo ""
    info "This will:"
    echo "  1. Disable the read-only overlay filesystem"
    echo "  2. Reboot the Pi (root becomes writable)"
    echo "  3. You then run: sudo ./update.sh apply"
    echo ""
    read -r -p "Continue? [y/N] " confirm
    if [[ ! "${confirm}" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi

    # Back up config before any changes
    if [[ -f /data/config/config.ini ]]; then
        cp /data/config/config.ini /data/config/config.ini.pre-update.save
        success "Config backed up to /data/config/config.ini.pre-update.save"
    fi

    write_state "apply_pending"

    info "Disabling overlay filesystem..."
    raspi-config nonint disable_overlayfs
    success "Overlay disabled — will take effect after reboot"

    echo ""
    echo -e "${BOLD}${YEL}Reboot required.${RST}"
    echo  "  After reboot, run: sudo ./update.sh apply"
    echo ""
    read -r -p "Reboot now? [y/N] " confirm
    if [[ "${confirm}" =~ ^[Yy]$ ]]; then
        reboot
    fi
}

# =============================================================================
# Phase 2 — Apply: pull, setup, validate, re-enable overlay
# =============================================================================

phase_apply() {
    header "Update Apply — Phase 2 of 3"

    PHASE=$(current_phase)
    if [[ "${PHASE}" == "none" ]] && overlay_is_enabled; then
        error "Overlay is still enabled and no prepare phase was run."
        error "Run: sudo ./update.sh prepare"
        exit 1
    fi

    echo ""
    info "This will:"
    echo "  1. Stop all FreezerPi services and watchdog"
    echo "  2. Pull latest changes from GitHub"
    echo "  3. Run setup.sh"
    echo "  4. Validate the install"
    echo "  5. Re-enable the overlay filesystem"
    echo "  6. Reboot"
    echo ""
    read -r -p "Continue? [y/N] " confirm
    if [[ ! "${confirm}" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi

    # Stop services safely
    header "Stopping Services"
    systemctl stop watchdog 2>/dev/null || true
    for svc in "${SERVICES[@]}"; do
        systemctl stop "${svc}.service" 2>/dev/null || true
        info "Stopped: ${svc}"
    done

    # Pull latest — run as the repo owner, not root (root has no SSH key)
    header "Pulling Latest Changes"
    REPO_OWNER=$(stat -c '%U' "${SCRIPT_DIR}/.git" 2>/dev/null || echo "pi")
    OLD_VERSION=$(cat "${SCRIPT_DIR}/VERSION" 2>/dev/null || echo "unknown")
    if sudo -u "${REPO_OWNER}" git -C "${SCRIPT_DIR}" pull --ff-only; then
        NEW_VERSION=$(cat "${SCRIPT_DIR}/VERSION" 2>/dev/null || echo "unknown")
        success "Updated: ${OLD_VERSION} → ${NEW_VERSION}"
    else
        error "git pull failed. Is the Pi online and SSH key configured for ${REPO_OWNER}?"
        error "You can pull manually first (git pull) and then re-run: sudo ./update.sh apply"
        exit 1
    fi

    # Run setup
    header "Running Setup"
    bash "${SCRIPT_DIR}/setup.sh"

    # Validate before re-enabling overlay
    header "Validating Install"

    # Start services to validate
    for svc in "${SERVICES[@]}"; do
        systemctl start "${svc}.service" 2>/dev/null || true
    done
    sleep 5  # Give services a moment to initialize

    if validate_services; then
        success "Validation passed"
        write_state "verify_pending"

        # Re-enable overlay
        header "Re-enabling Overlay Filesystem"
        raspi-config nonint enable_overlayfs
        success "Overlay re-enabled — will take effect after reboot"

        echo ""
        echo -e "${BOLD}${GRN}Update applied successfully.${RST}"
        echo  "  Version: ${NEW_VERSION}"
        echo  "  After reboot, run: sudo ./update.sh verify"
        echo ""
        read -r -p "Reboot now? [y/N] " confirm
        if [[ "${confirm}" =~ ^[Yy]$ ]]; then
            reboot
        fi
    else
        warn "Validation failed. Overlay NOT re-enabled."
        warn "The system is running but the overlay is still disabled."
        warn "Investigate the issues above, then manually run:"
        warn "  sudo raspi-config  → Performance Options → Overlay → Enable"
        write_state "apply_failed"
        exit 1
    fi
}

# =============================================================================
# Phase 3 — Verify: confirm everything healthy after second reboot
# =============================================================================

phase_verify() {
    header "Update Verify — Phase 3 of 3"

    PHASE=$(current_phase)
    if [[ "${PHASE}" != "verify_pending" ]]; then
        warn "No pending verification found (state: ${PHASE})."
        info "Running validation anyway..."
    fi

    if validate_services; then
        VERSION=$(cat /opt/freezerpi/VERSION 2>/dev/null || echo "unknown")
        echo ""
        echo -e "${BOLD}${GRN}============================================================${RST}"
        echo -e "${BOLD}${GRN}  Update complete. Version ${VERSION} is running.${RST}"
        echo -e "${BOLD}${GRN}============================================================${RST}"
        rm -f "${STATE_FILE}"
        success "Update state cleared"
    else
        error "Validation failed after reboot."
        error "Check service logs: journalctl -u freezer-alert.service -f"
        exit 1
    fi
}

# =============================================================================
# Status — show current state
# =============================================================================

phase_status() {
    echo ""
    echo -e "${BOLD}FreezerPi Update Status${RST}"
    echo ""

    echo -e "${BOLD}Overlay filesystem:${RST}"
    if overlay_is_enabled; then
        echo -e "  ${GRN}Enabled${RST} (root is read-only)"
    else
        echo -e "  ${YEL}Disabled${RST} (root is writable)"
    fi

    echo ""
    echo -e "${BOLD}Installed version:${RST}"
    echo "  $(cat /opt/freezerpi/VERSION 2>/dev/null || echo 'unknown')"

    echo ""
    echo -e "${BOLD}Repo version:${RST}"
    echo "  $(cat "${SCRIPT_DIR}/VERSION" 2>/dev/null || echo 'unknown')"

    echo ""
    echo -e "${BOLD}Update state:${RST}"
    if [[ -f "${STATE_FILE}" ]]; then
        cat "${STATE_FILE}" | sed 's/^/  /'
    else
        echo "  No update in progress"
    fi

    echo ""
    echo -e "${BOLD}Service status:${RST}"
    for svc in "${SERVICES[@]}" watchdog; do
        STATUS=$(systemctl is-active "${svc}.service" 2>/dev/null || echo "inactive")
        if [[ "${STATUS}" == "active" ]]; then
            echo -e "  ${GRN}●${RST} ${svc}"
        else
            echo -e "  ${RED}●${RST} ${svc} (${STATUS})"
        fi
    done
    echo ""
}

# =============================================================================
# Entrypoint
# =============================================================================

case "${1:-}" in
    prepare) phase_prepare ;;
    apply)   phase_apply   ;;
    verify)  phase_verify  ;;
    status)  phase_status  ;;
    *)
        echo ""
        echo -e "${BOLD}Usage:${RST} sudo ./update.sh <phase>"
        echo ""
        echo "  prepare  — Phase 1: disable overlay filesystem and reboot"
        echo "  apply    — Phase 2: pull latest, run setup, validate, re-enable overlay, reboot"
        echo "  verify   — Phase 3: confirm services healthy after reboot"
        echo "  status   — Show overlay state, installed version, and service health"
        echo ""
        echo -e "${BOLD}Full update cycle:${RST}"
        echo "  sudo ./update.sh prepare   # reboots"
        echo "  sudo ./update.sh apply     # reboots"
        echo "  sudo ./update.sh verify"
        echo ""
        echo -e "${BOLD}Without overlay enabled:${RST}"
        echo "  sudo ./update.sh apply"
        echo "  sudo ./update.sh verify"
        echo ""
        exit 1
        ;;
esac
