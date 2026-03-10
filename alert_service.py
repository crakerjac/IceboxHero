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
  - DB corruption flag (/run/freezerpi/db_corrupted.flag): consumed once and converted to an email.
"""

import os
import time
import json
import smtplib
import threading
import urllib.request
from email.message import EmailMessage
from gpiozero import Buzzer, Button
from config_helper import load_config

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

config = load_config()

IPC_FILE                 = "/run/freezerpi/telemetry_state.json"
STALE_THRESHOLD_SECONDS  = config.getint('display', 'stale_timeout')
SILENCE_DURATION_SECONDS = config.getint('alerts', 'silence_duration')
EMAIL_COOLDOWN_SECONDS   = config.getint('alerts', 'email_cooldown')
NTP_SYNC_YEAR            = config.getint('system', 'ntp_sync_year')
FREEZE_THRESHOLD         = config.getint('alerts', 'sensor_freeze_seconds')
EMAIL_ALIVE_URL          = config.get('network', 'email_alive_url', fallback='')
MAX_EMAIL_QUEUE          = 100

# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------

buzzer         = Buzzer(config.getint('hardware', 'buzzer_pin'))
silence_button = Button(config.getint('hardware', 'button_pin'), pull_up=True)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

silence_until_timestamp = 0
silence_lock            = threading.Lock()   # Protects silence_until_timestamp

email_queue             = []
queue_lock              = threading.Lock()
last_email_sent_times   = {}  # {"sensor_ALERTTYPE": monotonic_timestamp}
critical_read_counts    = {}  # {"sensor_name": consecutive_critical_count}
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
    buzzer.off()


silence_button.when_pressed = silence_callback


# ---------------------------------------------------------------------------
# Email queue
# ---------------------------------------------------------------------------

def queue_email(alert_type, sensor_name, current_temp, ignore_cooldown=False, status_email=False):
    """Enforces per-event cooldowns and appends an email to the retry queue."""
    event_key = f"{sensor_name}_{alert_type}"
    now_mono  = time.monotonic()
    now_real  = time.time()

    if not ignore_cooldown:
        last_sent = last_email_sent_times.get(event_key, 0)
        if (now_mono - last_sent) < EMAIL_COOLDOWN_SECONDS:
            return

    prefix  = "[STATUS] " if status_email else "[ALERT] "
    subject = f"{prefix}Freezer Monitor {alert_type}: {sensor_name}"
    body    = (
        f"Event detected for {sensor_name}.\n"
        f"Type: {alert_type}\n"
        f"Current Reading: {current_temp}F\n"
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

    # Fire the system boot notification once NTP is confirmed
    queue_email("SYSTEM_BOOT", "Monitor", "System Online", ignore_cooldown=True, status_email=True)

    smtp_server_addr = config.get('email', 'smtp_server')
    smtp_port        = config.getint('email', 'smtp_port')
    smtp_user        = config.get('email', 'smtp_user')
    smtp_pass        = config.get('email', 'smtp_pass')
    recipient        = config.get('email', 'recipient')

    while True:
        global email_queue

        with queue_lock:
            items_to_send = list(email_queue) if email_queue else []

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

            # Rebuild queue: keep failed items and any new items added during send
            with queue_lock:
                sent_ids  = {id(i) for i in items_to_send if i not in failed_items}
                email_queue = failed_items + [i for i in email_queue if id(i) not in sent_ids]

        time.sleep(300)  # 5 minutes


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

    DB_CORRUPT_FLAG   = "/run/freezerpi/db_corrupted.flag"
    last_ipc_timestamp = 0

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
                    for name, temp in sensor_data.items():
                        if temp is None:
                            trigger_buzzer = True
                            if is_new_read:
                                queue_email("FAILURE", name, "MISSING/READ ERROR")
                                critical_read_counts[name] = 0
                        else:
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
                                        queue_email("WARNING", name, temp)

            except (json.JSONDecodeError, KeyError):
                pass  # Handled gracefully on the next loop iteration

        # --- Buzzer control ---
        with silence_lock:
            is_silenced = time.monotonic() <= silence_until_timestamp

        if trigger_buzzer:
            if not is_silenced and not buzzer.is_active:
                buzzer.on()
        else:
            if buzzer.is_active:
                buzzer.off()

        time.sleep(1)


if __name__ == '__main__':
    main()
