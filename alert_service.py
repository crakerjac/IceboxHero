"""
Module 4 — Hardware Alerts & Email Queue (alert_service.py)

Manages the physical buzzer, GPIO silence button, and asynchronous SMTP
email queue. The email processor runs in a background thread so that
network timeouts (30–60 s) never block the buzzer or button response.

Key behaviors:
  - Buzzer fires on: CRITICAL temp (2 consecutive reads), missing sensor, stale data.
  - Silence button (GPIO interrupt): mutes buzzer for 1 hour; alarm re-arms automatically.
  - Email queue: in-memory, up to 100 items; retried every 5 minutes until sent.
  - 60-minute cooldown per alert type prevents email flooding.
  - [ALERT] prefix for actionable alerts; [STATUS] prefix for informational boots.
  - email_alive ping fires after each successful send to verify SMTP health independently.
  - Sensor freeze detection: buzzer triggers if IPC monotonic clock stops advancing.
  - DB corruption flag (/run/iceboxhero/db_corrupted.flag): consumed once and converted to an email.
  - Watchdog reboot detection: watchdog_repair.sh sets pending_email flag in
    /data/config/alert_state.json before reboot. On next boot, alert_service reads
    the flag post-NTP and sends [ALERT] WATCHDOG_REBOOT with cooldown to suppress flooding.
"""

import os
import time
import json
import smtplib
import threading
import urllib.request
from email.message import EmailMessage
from gpiozero import Buzzer
import RPi.GPIO as _RPIGPIO
from config_helper import load_config

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _config_error_alert(error):
    """
    Last-resort handler for config load failure.
    Sounds the buzzer on the hardcoded pin and loops forever so:
      - systemd sees the service as 'running' (no restart loop)
      - the buzzer keeps sounding until someone investigates
      - healthchecks.io heartbeat stops → email arrives after timeout
    The silence button is intentionally not supported here — config is
    unreadable so we don't know which GPIO it's on.
    """
    BUZZER_PIN = 17  # Hardcoded fallback — matches default config
    print(f"FATAL: config load failed: {error}")
    print(f"Sounding buzzer on GPIO{BUZZER_PIN}. Fix /data/config/config.ini and restart.")
    try:
        bz = Buzzer(BUZZER_PIN)
        bz.on()
    except Exception as bz_err:
        print(f"Buzzer init also failed: {bz_err}. Looping silently.")
    while True:
        time.sleep(60)


try:
    config = load_config()
except Exception as e:
    _config_error_alert(e)

IPC_FILE                 = "/run/iceboxhero/telemetry_state.json"
ALERT_STATE_FILE         = "/data/config/alert_state.json"
STALE_THRESHOLD_SECONDS  = config.getint('display', 'stale_timeout')
SILENCE_DURATION_SECONDS = config.getint('alerts', 'silence_duration')
EMAIL_COOLDOWN_SECONDS   = config.getint('alerts', 'email_cooldown')
NTP_SYNC_YEAR            = config.getint('system', 'ntp_sync_year')
FREEZE_THRESHOLD         = config.getint('alerts', 'sensor_freeze_seconds')
CHECKIN_INTERVAL_DAYS    = config.getint('alerts', 'checkin_interval_days', fallback=30)
EMAIL_ALIVE_URL          = config.get('network', 'email_alive_url', fallback='')
MAX_EMAIL_QUEUE          = 100

# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------

try:
    buzzer = Buzzer(config.getint('hardware', 'buzzer_pin'))
    print("Buzzer initialized.")
except Exception as e:
    print(f"WARNING: Buzzer init failed (no hardware?): {e}. Buzzer disabled.")
    buzzer = None

try:
    _btn_pin = config.getint('hardware', 'button_pin')
    _RPIGPIO.setmode(_RPIGPIO.BCM)
    _RPIGPIO.setwarnings(False)
    _RPIGPIO.setup(_btn_pin, _RPIGPIO.IN, pull_up_down=_RPIGPIO.PUD_UP)
    silence_button = _btn_pin
    print(f"Silence button initialized on GPIO{_btn_pin} (polling mode).")
except Exception as e:
    print(f"WARNING: Silence button init failed: {e}. Button disabled.")
    silence_button = None

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

silence_until_timestamp = 0
silence_lock            = threading.Lock()   # Protects silence_until_timestamp

email_queue             = []
queue_lock              = threading.Lock()
last_email_sent_times   = {}  # {"sensor_ALERTTYPE": monotonic_timestamp}
critical_read_counts    = {}  # {"sensor_name": consecutive_critical_count}
sensor_failed_state     = {}  # {"sensor_name": bool} — True while sensor is reporting None
sensor_warning_state    = {}  # {"sensor_name": bool} — True while sensor is in warning zone
last_freeze_email       = 0


# ---------------------------------------------------------------------------
# GPIO interrupt
# ---------------------------------------------------------------------------

def silence_callback():
    """Hardware interrupt: mutes buzzer for silence_duration seconds."""
    global silence_until_timestamp
    with silence_lock:
        silence_until_timestamp = time.monotonic() + SILENCE_DURATION_SECONDS
    print(f"Silence button pressed. Muting buzzer for {SILENCE_DURATION_SECONDS} seconds.")
    if buzzer:
        buzzer.off()


def _button_poll_loop():
    """Poll the silence button at 50 Hz — avoids kernel edge detection issues."""
    last_state = True  # pull_up=True means unpressed=HIGH
    while True:
        try:
            state = bool(_RPIGPIO.input(silence_button))
            if last_state and not state:   # HIGH→LOW = button pressed
                silence_callback()
            last_state = state
        except Exception:
            pass
        time.sleep(0.02)

if silence_button is not None:
    _btn_thread = threading.Thread(target=_button_poll_loop, daemon=True)
    _btn_thread.start()


# ---------------------------------------------------------------------------
# Email queue
# ---------------------------------------------------------------------------

def queue_email(alert_type, sensor_name, current_temp, ignore_cooldown=False, status_email=False):
    """Enforces per-event cooldowns and appends an email to the retry queue."""
    event_key = f"{sensor_name}_{alert_type}"
    now_mono  = time.monotonic()
    now_real  = time.time()

    if not ignore_cooldown:
        # Default to -EMAIL_COOLDOWN_SECONDS so first occurrence always fires.
        # time.monotonic() starts at ~uptime seconds, so defaulting to 0 would
        # incorrectly suppress alerts for the first hour after every boot.
        last_sent = last_email_sent_times.get(event_key, -EMAIL_COOLDOWN_SECONDS)
        if (now_mono - last_sent) < EMAIL_COOLDOWN_SECONDS:
            return

    prefix  = "[STATUS] " if status_email else "[ALERT] "
    subject = f"{prefix}IceboxHero {alert_type}: {sensor_name}"

    # Only append F unit for numeric readings — status messages pass plain strings
    reading = f"{current_temp}F" if isinstance(current_temp, (int, float)) else current_temp

    body    = (
        f"Event detected for {sensor_name}.\n"
        f"Type: {alert_type}\n"
        f"Current Reading: {reading}\n"
        f"Time: {time.ctime(now_real)}"
    )

    with queue_lock:
        if len(email_queue) < MAX_EMAIL_QUEUE:
            email_queue.append({"subject": subject, "body": body})
            last_email_sent_times[event_key] = now_mono
            print(f"Queued email: {subject}")
        else:
            print(f"WARNING: Email queue full ({MAX_EMAIL_QUEUE}), dropping: {subject}")


def process_email_queue():
    """Background thread: sends queued emails every 5 minutes via SMTP SSL."""
    wait_for_ntp_sync()

    # Fire the appropriate boot notification once NTP is confirmed.
    # BOOT_FLAG prevents duplicate emails if the service restarts mid-boot.
    BOOT_FLAG = "/run/iceboxhero/boot_email_sent"
    if not os.path.exists(BOOT_FLAG):
        try:
            open(BOOT_FLAG, 'w').close()
        except OSError:
            pass

        alert_state = read_alert_state()

        # --- Watchdog reboot detection ---
        # watchdog_repair.sh sets pending_email=True before reboot if outside cooldown.
        # We clear the flag here and record the send time post-NTP.
        if alert_state.get("watchdog_reboot_pending_email", False):
            write_alert_state({
                "watchdog_reboot_pending_email": False,
                "watchdog_reboot_last_email": time.time()
            })
            queue_email("WATCHDOG_REBOOT", "System",
                        "Hardware watchdog triggered a reboot — sensor IPC file was stale.",
                        ignore_cooldown=True)
            print("Watchdog reboot detected — queued WATCHDOG_REBOOT email.")
        else:
            queue_email("SYSTEM_BOOT", "Monitor", "System Online",
                        ignore_cooldown=True, status_email=True)

        # --- Monthly checkin email ---
        # Fires once per checkin_interval_days to confirm email pipeline is working.
        last_checkin = alert_state.get("last_checkin_email", 0.0)
        checkin_interval_seconds = CHECKIN_INTERVAL_DAYS * 86400
        if (time.time() - last_checkin) >= checkin_interval_seconds:
            write_alert_state({"last_checkin_email": time.time()})
            queue_email("CHECKIN", "System",
                        f"IceboxHero is running normally. Next checkin in {CHECKIN_INTERVAL_DAYS} days.",
                        ignore_cooldown=True, status_email=True)
            print(f"Queued {CHECKIN_INTERVAL_DAYS}-day checkin email.")

    smtp_server_addr = config.get('email', 'smtp_server')
    smtp_port        = config.getint('email', 'smtp_port')
    smtp_user        = config.get('email', 'smtp_user')
    smtp_pass        = config.get('email', 'smtp_pass')
    recipient        = config.get('email', 'recipient')

    while True:
        global email_queue

        with queue_lock:
            items_to_send = list(email_queue) if email_queue else []
            # Clear items we're about to attempt — new alerts can still be added
            # to email_queue during the send loop and won't be lost.
            email_queue = [i for i in email_queue if i not in items_to_send]

        if items_to_send:
            failed_items = []

            try:
                with smtplib.SMTP_SSL(smtp_server_addr, smtp_port) as server:
                    server.login(smtp_user, smtp_pass)

                    for item in items_to_send:
                        msg              = EmailMessage()
                        msg['Subject']   = item["subject"]
                        msg['From']      = smtp_user
                        msg['To']        = recipient
                        msg.set_content(item["body"])

                        try:
                            server.send_message(msg)
                            print(f"Sent: {item['subject']}")
                            if EMAIL_ALIVE_URL:
                                try:
                                    urllib.request.urlopen(EMAIL_ALIVE_URL, timeout=5)
                                except Exception:
                                    pass  # Ping failure must never break the email loop
                        except Exception as e:
                            print(f"Failed to send '{item['subject']}': {e}")
                            failed_items.append(item)

            except Exception as e:
                print(f"SMTP connection failed, retrying in 5 min: {e}")
                failed_items = items_to_send  # Retry the whole batch

            # Re-queue any failed items at the front for next attempt
            if failed_items:
                with queue_lock:
                    email_queue = failed_items + email_queue

        time.sleep(300)  # 5 minutes


# ---------------------------------------------------------------------------
# Alert state (persistent across reboots via /data/config/alert_state.json)
# ---------------------------------------------------------------------------

def read_alert_state():
    """Read alert_state.json — returns defaults if missing or corrupt."""
    try:
        with open(ALERT_STATE_FILE, 'r') as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("corrupt")
        return state
    except Exception:
        return {"watchdog_reboot_pending_email": False, "watchdog_reboot_last_email": 0.0, "last_checkin_email": 0.0}


def write_alert_state(state):
    """Atomically write alert_state.json. Preserves existing keys."""
    try:
        existing = read_alert_state()
        existing.update(state)
        tmp = ALERT_STATE_FILE + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(existing, f)
        os.replace(tmp, ALERT_STATE_FILE)
    except Exception as e:
        print(f"WARNING: Failed to write alert_state: {e}")


# ---------------------------------------------------------------------------
# NTP sync gate
# ---------------------------------------------------------------------------

def wait_for_ntp_sync():
    """Blocks until the system clock year reaches ntp_sync_year."""
    print("Checking system clock synchronization...")
    while time.gmtime().tm_year < NTP_SYNC_YEAR:
        print("Clock unsynced. Waiting for NTP...")
        time.sleep(5)
    print("Clock synchronized.")


# ---------------------------------------------------------------------------
# Safe JSON reader
# ---------------------------------------------------------------------------

def safe_read_json(path, retries=3):
    for _ in range(retries):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    global last_freeze_email
    print("Starting Hardware Alert & Email Service...")

    temp_warning  = config.getfloat('sampling', 'temp_warning')
    temp_critical = config.getfloat('sampling', 'temp_critical')

    email_thread = threading.Thread(target=process_email_queue, daemon=True)
    email_thread.start()

    DB_CORRUPT_FLAG    = "/run/iceboxhero/db_corrupted.flag"
    last_ipc_timestamp = 0
    # Suppress sensor FAILURE alerts for the first 60 seconds after startup.
    # sensor_service needs time to get first 1-wire readings — during this window
    # it writes null values that would otherwise trigger spurious FAILURE emails.
    startup_grace_until = time.monotonic() + 60

    while True:
        is_stale      = False
        trigger_buzzer = False

        # --- DB corruption flag ---
        if os.path.exists(DB_CORRUPT_FLAG):
            queue_email("SYSTEM_ERROR", "Database", "Corruption detected and auto-recovered.")
            try:
                os.remove(DB_CORRUPT_FLAG)
            except OSError:
                pass

        # --- Read IPC state ---
        if os.path.exists(IPC_FILE):
            mtime = os.path.getmtime(IPC_FILE)
            if (time.time() - mtime) > STALE_THRESHOLD_SECONDS:
                is_stale       = True
                trigger_buzzer = True
                queue_email("CRITICAL_STALE_DATA", "System", "--.-F")

            try:
                payload = safe_read_json(IPC_FILE)

                if payload is None:
                    pass  # Fall through to buzzer evaluation on next iteration
                else:
                    sensor_data   = payload.get("sensors", {})
                    ipc_timestamp = payload.get("timestamp", 0)
                    ipc_monotonic = payload.get("monotonic", None)

                    # Sensor service freeze detection
                    if ipc_monotonic is not None:
                        delta = time.monotonic() - ipc_monotonic
                        if delta > FREEZE_THRESHOLD:
                            trigger_buzzer = True
                            if (time.monotonic() - last_freeze_email) > EMAIL_COOLDOWN_SECONDS:
                                queue_email("SYSTEM_FREEZE", "Sensor Service", "No updates detected")
                                last_freeze_email = time.monotonic()

                    is_new_read = (ipc_timestamp != last_ipc_timestamp)
                    if is_new_read:
                        last_ipc_timestamp = ipc_timestamp

                    # --- Evaluate temperature alerts ---
                    in_grace = time.monotonic() < startup_grace_until
                    for name, temp in sensor_data.items():
                        if temp is None:
                            trigger_buzzer = True
                            if is_new_read and not in_grace:
                                sensor_failed_state[name] = True
                                sensor_warning_state[name] = False
                                queue_email("FAILURE", name, "MISSING/READ ERROR")
                                critical_read_counts[name] = 0
                        else:
                            # Sensor recovered from a previous FAILURE state
                            if is_new_read and sensor_failed_state.get(name, False):
                                sensor_failed_state[name] = False
                                sensor_warning_state[name] = False
                                queue_email("RECOVERED", name, temp, status_email=True)

                            if temp >= temp_critical:
                                if is_new_read:
                                    critical_read_counts[name] = critical_read_counts.get(name, 0) + 1
                                    if critical_read_counts[name] >= 2:
                                        queue_email("CRITICAL", name, temp)
                                if critical_read_counts.get(name, 0) >= 2:
                                    trigger_buzzer = True
                            else:
                                if is_new_read:
                                    critical_read_counts[name] = 0
                                    if temp >= temp_warning:
                                        # Only email on transition INTO warning state
                                        if not sensor_warning_state.get(name, False):
                                            sensor_warning_state[name] = True
                                            queue_email("WARNING", name, temp)
                                    else:
                                        sensor_warning_state[name] = False

            except (json.JSONDecodeError, KeyError):
                pass  # Handled gracefully on the next loop iteration

        # --- Buzzer control ---
        with silence_lock:
            is_silenced = time.monotonic() <= silence_until_timestamp

        if buzzer:
            if trigger_buzzer:
                if is_silenced:
                    if buzzer.is_active:
                        buzzer.off()   # Explicitly silence even when alarm condition persists
                elif not buzzer.is_active:
                    buzzer.on()
            else:
                if buzzer.is_active:
                    buzzer.off()

        time.sleep(1)


if __name__ == '__main__':
    main()
