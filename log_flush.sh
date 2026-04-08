#!/bin/bash
# IceboxHero — Log Flush
#
# Called on clean shutdown or reboot via icebox-logflush.service.
# Dumps the current boot's journal for all IceboxHero services to a
# timestamped file in /data/logs/, then rotates older files keeping
# the last 3 boots. This ensures the failure history is available
# via the web dashboard download button even after a reboot.

LOG_DIR="/data/logs"
MAX_BOOTS=3

mkdir -p "${LOG_DIR}"

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
OUTFILE="${LOG_DIR}/icebox_boot_${TIMESTAMP}.log"

# Write header
{
    echo "=================================================="
    echo "IceboxHero Boot Log"
    echo "Captured: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "Hostname: $(hostname)"
    echo "Uptime:   $(uptime)"
    echo "Version:  $(cat /opt/iceboxhero/VERSION 2>/dev/null || echo unknown)"
    echo "=================================================="
    echo ""
} > "${OUTFILE}"

# Dump journal for all icebox services — current boot only
for svc in icebox-sensor icebox-display icebox-alert icebox-db icebox-web \
           icebox-watchdog icebox-netwatchdog; do
    echo "--------------------------------------------------" >> "${OUTFILE}"
    echo "Service: ${svc}.service" >> "${OUTFILE}"
    echo "--------------------------------------------------" >> "${OUTFILE}"
    journalctl -u "${svc}.service" -b --no-pager 2>/dev/null >> "${OUTFILE}" || \
        echo "(no journal entries)" >> "${OUTFILE}"
    echo "" >> "${OUTFILE}"
done

echo "Log saved: ${OUTFILE}" >&2

# Rotate — keep only the last MAX_BOOTS files
mapfile -t old_logs < <(ls -t "${LOG_DIR}"/icebox_boot_*.log 2>/dev/null | tail -n +$((MAX_BOOTS + 1)))
for f in "${old_logs[@]}"; do
    rm -f "${f}"
    echo "Rotated out: ${f}" >&2
done
