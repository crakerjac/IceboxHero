#!/bin/bash
# IceboxHero — Watchdog Repair Hook
#
# Invoked by /usr/sbin/watchdog (as repair-binary) the moment it detects
# /run/iceboxhero/telemetry_state.json has gone stale (>180s unchanged).
# This runs seconds before the hardware watchdog forces a reboot — it is
# the only chance to act before power is cut to the running system.
#
# TIMING: the daemon runs this synchronously and generally cannot pet the
# hardware watchdog (watchdog-timeout=15s) while it does. repair-timeout
# is set to 8s in watchdog.conf specifically to leave the daemon a wide
# margin to regain control and feed the hardware timer before it could
# fire on its own. Because of that tight budget, priority order matters:
# the DB backup (the thing we most want to survive the reset) runs FIRST,
# unconditionally. The journal snapshot is best-effort and runs after —
# if we're already tight on time, it's fine to lose the log, not the data.
#
# This performs ONE-TIME writes to the SD card only when the watchdog has
# already decided a reset is imminent — no periodic/continuous SD writes
# are introduced by this script.
#
# Exit code is always 1 ("repair failed") — nothing here can actually
# un-stick a hung sensor service, so the hardware reboot must proceed.

set -u

logger -t icebox-watchdog-repair "Watchdog trigger fired — IPC stale, reset imminent. Running emergency backup."

# --- Priority 1: emergency RAM -> SD database backup -----------------------
# Reuses the existing backup_ram_db_to_disk() path so this stays a single
# well-tested code path rather than a second implementation. Atomic
# (tmp file + os.replace), so a hard cutoff mid-write can't corrupt the
# existing on-disk copy — worst case, this attempt simply doesn't land.
python3 - <<'PYEOF' 2>&1 | logger -t icebox-watchdog-repair
import sys
sys.path.insert(0, '/opt/iceboxhero')
import configparser
from db_logger import backup_ram_db_to_disk

config = configparser.ConfigParser()
config.read('/data/config/config.ini')
try:
    retention_days = config.getint('database', 'retention_days')
except Exception:
    retention_days = 45  # safe fallback if config is unreadable at this point

backup_ram_db_to_disk(retention_days)
print("Emergency backup attempt complete.")
PYEOF

# --- Priority 2: best-effort pre-reboot log snapshot ------------------------
# Only reached if the backup above left any time in the budget. Captures
# the last 5 minutes per service so the crash is diagnosable after reboot
# even though it never goes through a clean shutdown (ExecStop in
# icebox-logflush.service never runs on a hardware reset).
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
OUTFILE="/data/logs/icebox_watchdog_trigger_${TIMESTAMP}.log"
mkdir -p /data/logs

{
    echo "=================================================="
    echo "WATCHDOG-TRIGGERED RESET — pre-reboot snapshot"
    echo "Captured:  $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "Hostname:  $(hostname)"
    echo "Uptime:    $(uptime)"
    echo "IPC file:  $(stat -c 'mtime=%y size=%s' /run/iceboxhero/telemetry_state.json 2>&1)"
    echo "=================================================="
    for svc in icebox-sensor icebox-display icebox-alert icebox-db icebox-web; do
        echo ""
        echo "--- ${svc}.service (last 5 min) ---"
        journalctl -u "${svc}.service" --since "-5min" --no-pager 2>/dev/null || echo "(no journal entries)"
    done
} > "${OUTFILE}" 2>&1

logger -t icebox-watchdog-repair "Pre-reboot snapshot written: ${OUTFILE}. Hardware reboot will proceed."

exit 1
