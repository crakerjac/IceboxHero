#!/bin/bash
# IceboxHero — Watchdog Repair Script
#
# Called by /usr/sbin/watchdog before triggering a hardware reboot.
# Checks the watchdog_reboot_last_email cooldown in alert_state.json and
# sets the pending_email flag if outside cooldown. alert_service reads
# this flag on next boot (post-NTP) and sends the WATCHDOG_REBOOT email.
#
# This script must exit 0 — a non-zero exit causes the watchdog to
# attempt repair again rather than proceeding to reboot.

ALERT_STATE="/data/config/alert_state.json"
# Must match email_cooldown in config.ini (default 3600 seconds)
COOLDOWN=3600

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') watchdog_repair: $*" | tee -a /data/logs/watchdog_repair.log
}

log "Repair script triggered — sensor IPC file stale."

# Read last email timestamp from alert_state.json
if [ -f "$ALERT_STATE" ]; then
    last_email=$(python3 -c "
import json, sys
try:
    d = json.load(open('$ALERT_STATE'))
    print(d.get('watchdog_reboot_last_email', 0))
except Exception:
    print(0)
" 2>/dev/null)
else
    last_email=0
fi

now=$(date +%s)
age=$((now - ${last_email%.*}))  # strip decimal

log "Last watchdog email: ${last_email} (${age}s ago, cooldown=${COOLDOWN}s)"

if [ "$age" -gt "$COOLDOWN" ]; then
    log "Outside cooldown — setting pending email flag."
    python3 - << PYEOF
import json, os

path = "$ALERT_STATE"
tmp  = path + ".tmp"

try:
    with open(path, 'r') as f:
        state = json.load(f)
    if not isinstance(state, dict):
        raise ValueError
except Exception:
    state = {}

state["watchdog_reboot_pending_email"] = True
# Leave watchdog_reboot_last_email unchanged — alert_service sets it post-NTP

with open(tmp, 'w') as f:
    json.dump(state, f)
os.replace(tmp, path)
print("alert_state.json updated: pending_email=True")
PYEOF
else
    log "Inside cooldown (${age}s < ${COOLDOWN}s) — suppressing email."
fi

log "Repair script complete — watchdog will now reboot."
exit 0
