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
CONFIG_FILE="/data/config/config.ini"
LOG_FILE="/data/logs/watchdog_repair.log"
mkdir -p "$(dirname "${LOG_FILE}")"
SERVICE_USER="pi"    # User that runs icebox-alert.service

# Read email_cooldown directly from config.ini — avoids hardcoded value drifting out of sync
COOLDOWN=$(awk -F'=' '/^email_cooldown/{gsub(/ /,"",$2); print $2}' "${CONFIG_FILE}" 2>/dev/null)
if [[ -z "${COOLDOWN}" ]]; then
    COOLDOWN=3600  # Fallback if config.ini is unreadable
    log "WARNING: Could not read email_cooldown from config.ini — using default ${COOLDOWN}s"
fi

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') watchdog_repair: $*" | tee -a "${LOG_FILE}"
}

log "Repair script triggered — sensor IPC file stale."

# Read last email timestamp
if [ -f "$ALERT_STATE" ]; then
    last_email=$(python3 -c "
import json
try:
    d = json.load(open('$ALERT_STATE'))
    print(d.get('watchdog_reboot_last_email', 0))
except Exception:
    print(0)
" 2>&1)
    log "Read last_email: ${last_email}"
else
    last_email=0
fi

now=$(date +%s)
age=$((now - ${last_email%.*}))

log "Last watchdog email: ${last_email} (${age}s ago, cooldown=${COOLDOWN}s)"

if [ "$age" -gt "$COOLDOWN" ]; then
    log "Outside cooldown — setting pending email flag."
    python3 -c "
import json, os, pwd
path = '$ALERT_STATE'
tmp  = path + '.tmp'
try:
    with open(path) as f:
        state = json.load(f)
    if not isinstance(state, dict):
        raise ValueError
except Exception:
    state = {}
state['watchdog_reboot_pending_email'] = True
with open(tmp, 'w') as f:
    json.dump(state, f)
try:
    pw = pwd.getpwnam('$SERVICE_USER')
    os.chown(tmp, pw.pw_uid, pw.pw_gid)
except Exception as e:
    print('WARNING: Could not restore ownership: ' + str(e))
os.replace(tmp, path)
print('alert_state.json updated: pending_email=True')
" 2>&1 | tee -a "${LOG_FILE}"
else
    log "Inside cooldown (${age}s < ${COOLDOWN}s) — suppressing email."
fi

log "Repair script complete — watchdog will now reboot."
exit 0
