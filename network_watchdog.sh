#!/bin/bash
# IceboxHero — Network Watchdog
#
# Runs every 5 minutes via systemd timer. Pings the default gateway to verify
# the Pi's network stack is healthy.
#
# Recovery sequence:
#   - 3 consecutive failed checks → restart NetworkManager (resets NM retry count)
#   - 3 more consecutive failures → reboot (up to 3 reboots total)
#   - After 3 reboots with no recovery → stop rebooting, retry NM only
#     (healthchecks.io dead-man will fire naturally)
#
# Noteworthy events logged to journald (captured to SD on shutdown by icebox-logflush.service).
# Persistent counters stored in /data/config/system_state.json.

STATE_FILE="/data/config/system_state.json"
PING_COUNT=3
PING_TIMEOUT=5
NM_WAIT=30
NM_FAIL_THRESHOLD=3    # Consecutive failed checks before restarting NM
MAX_REBOOTS=3          # Max network-triggered reboots before giving up

log() {
    # Log to journald — captured to SD on clean shutdown by icebox-logflush.service
    logger -t "network_watchdog" "$*"
}

# Read a value from system_state.json — returns 0 if missing or unreadable
read_state() {
    local key=$1
    python3 -c "
import json, sys
try:
    d = json.load(open('${STATE_FILE}'))
    print(d.get('${key}', 0))
except Exception:
    print(0)
" 2>/dev/null || echo 0
}

# Write a key=value pair into system_state.json, preserving other keys
write_state() {
    local key=$1
    local value=$2
    python3 -c "
import json, os
path = '${STATE_FILE}'
tmp  = path + '.tmp'
try:
    with open(path) as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}
state['${key}'] = ${value}
with open(tmp, 'w') as f:
    json.dump(state, f, indent=2)
os.replace(tmp, path)
" 2>/dev/null
}

reset_network_state() {
    python3 -c "
import json, os
path = '${STATE_FILE}'
tmp  = path + '.tmp'
try:
    with open(path) as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}
state['net_fail_count']   = 0
state['net_reboot_count'] = 0
with open(tmp, 'w') as f:
    json.dump(state, f, indent=2)
os.replace(tmp, path)
" 2>/dev/null
}

# Get default gateway IP dynamically
GATEWAY=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')

if [[ -z "${GATEWAY}" ]]; then
    log "WARNING: No default gateway detected — network interface may be down."
    GATEWAY=""
fi

# Attempt ping
ping_ok=false
if [[ -n "${GATEWAY}" ]] && ping -c "${PING_COUNT}" -W "${PING_TIMEOUT}" "${GATEWAY}" > /dev/null 2>&1; then
    ping_ok=true
fi

if [[ "${ping_ok}" == "true" ]]; then
    # Network healthy — reset counters silently
    fail_count=$(read_state "net_fail_count")
    if [[ "${fail_count}" -gt 0 ]]; then
        log "Network recovered (gateway ${GATEWAY} reachable). Resetting counters."
        reset_network_state
    fi
    exit 0
fi

# Network unreachable — increment failure count
fail_count=$(read_state "net_fail_count")
fail_count=$((fail_count + 1))
write_state "net_fail_count" "${fail_count}"
log "Gateway unreachable (attempt ${fail_count}/${NM_FAIL_THRESHOLD}). Gateway: ${GATEWAY:-unknown}"

if [[ "${fail_count}" -lt "${NM_FAIL_THRESHOLD}" ]]; then
    log "Waiting for next check cycle before taking action."
    exit 0
fi

# Hit NM threshold — restart NetworkManager
log "Restarting NetworkManager (${fail_count} consecutive failures)..."
systemctl restart NetworkManager
sleep "${NM_WAIT}"
write_state "net_fail_count" "0"

# Check if recovery worked
GATEWAY=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')
if [[ -n "${GATEWAY}" ]] && ping -c "${PING_COUNT}" -W "${PING_TIMEOUT}" "${GATEWAY}" > /dev/null 2>&1; then
    log "Network recovered after NetworkManager restart."
    reset_network_state
    exit 0
fi

# NM restart didn't help — check reboot count
reboot_count=$(read_state "net_reboot_count")
log "Network still unreachable after NetworkManager restart. Reboot count: ${reboot_count}/${MAX_REBOOTS}"

if [[ "${reboot_count}" -ge "${MAX_REBOOTS}" ]]; then
    log "WARNING: Max reboots (${MAX_REBOOTS}) reached — will not reboot again."
    log "Retrying NetworkManager only. healthchecks.io dead-man will fire if system is truly offline."
    exit 0
fi

# Reboot
reboot_count=$((reboot_count + 1))
write_state "net_reboot_count" "${reboot_count}"
log "Rebooting (network reboot ${reboot_count}/${MAX_REBOOTS})..."
reboot
