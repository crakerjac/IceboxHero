"""
Module 5 — Database Logger (db_logger.py)

Reads /run/iceboxhero/telemetry_state.json every 5 minutes and inserts valid readings
into a SQLite database that lives entirely in RAM (/run/icebox_db/).

SD card write strategy:
  - All INSERT operations go to the RAM database (zero SD wear).
  - A background thread backs up the RAM database to /data/db/ every 4 hours
    using SQLite's online backup API (atomic, no locking required).
  - On each backup, old rows beyond retention_days are pruned from RAM and
    the WAL file is truncated to reclaim memory.
  - On boot, the last SD backup is restored into RAM before the main loop starts.

Boot sequence:
  - verify_and_recover_db() — integrity check on SD backup, quarantine if corrupt
  - restore_db_from_backup() — load SD backup into RAM (no clock required)
  - init_db() — create schema in RAM DB (idempotent, no clock required)
  - wait_for_ntp_sync() — block writes until clock is valid
  - main loop + backup thread start

Integrity / NTP gates:
  - On boot, PRAGMA integrity_check runs against the SD backup; corruption
    triggers rename-to-.corrupt and sets /run/db_corrupted.flag for alert_service.
  - Writes are blocked until the system clock year >= ntp_sync_year to prevent
    1970-epoch timestamps being written to the database.
  - A heartbeat ping fires after each successful 5-minute write to healthchecks.io.
  - SIGTERM handler triggers a final backup before exit — no data lost on clean shutdown/reboot.
"""

import os
import json
import time
import signal
import sqlite3
import shutil
import threading
import urllib.request
from datetime import datetime
from config_helper import load_config, wait_for_ntp_sync, safe_read_json

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

DB_DIR          = "/data/db"
DB_FILE         = os.path.join(DB_DIR, "freezer_monitor.db")        # SD card backup
RAM_DB_DIR      = "/run/icebox_db"
RAM_DB_FILE     = os.path.join(RAM_DB_DIR, "freezer_monitor.db")    # Live runtime DB
IPC_FILE        = "/run/iceboxhero/telemetry_state.json"
DB_CORRUPT_FLAG = "/run/iceboxhero/db_corrupted.flag"
DATA_MOUNT_FLAG = "/run/iceboxhero/data_mount_lost.flag"

# ---------------------------------------------------------------------------
# RAM ↔ SD backup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# /data mount verification
# ---------------------------------------------------------------------------

def check_data_mount_valid():
    """Verifies /data is genuinely the mounted SD partition from this
    process's point of view — not an ephemeral stand-in directory that
    happens to live at the same path inside this service's private mount
    namespace (ProtectSystem=strict + ReadWritePaths=... /data).

    This matters because if that ever happens, every write in
    backup_ram_db_to_disk() below — including the last_backup timestamp
    file — succeeds without raising a single exception. Nothing looks
    wrong for as long as the service runs. The instant the box reboots,
    that ephemeral view is gone, and restore_db_from_backup() pulls
    whatever was actually last written to the real SD card, which may be
    far older than the "last_backup" timestamp ever suggested.

    Writes DATA_MOUNT_FLAG the moment the check fails so alert_service can
    raise it within minutes instead of this going unnoticed for months.
    Clears the flag if the mount is confirmed healthy again.
    """
    ok = os.path.ismount('/data')
    if not ok:
        print("CRITICAL: /data is not a real mountpoint from this service's view. "
              "Skipping backup to avoid silently writing to ephemeral storage.")
        try:
            with open(DATA_MOUNT_FLAG, 'w') as f:
                f.write(str(time.time()))
        except OSError as e:
            print(f"WARNING: Could not write {DATA_MOUNT_FLAG}: {e}")
    elif os.path.exists(DATA_MOUNT_FLAG):
        try:
            os.remove(DATA_MOUNT_FLAG)
            print("/data mount verified healthy again — cleared data_mount_lost flag.")
        except OSError:
            pass
    return ok


def backup_ram_db_to_disk(retention_days):
    """Atomically copies the live RAM database to the SD card, then prunes RAM."""
    if not check_data_mount_valid():
        return

    src = None
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        src = sqlite3.connect(RAM_DB_FILE, timeout=10)
        dst = sqlite3.connect(DB_FILE + ".tmp", timeout=10)
        try:
            src.backup(dst)
        finally:
            dst.close()

        os.replace(DB_FILE + ".tmp", DB_FILE)
        backup_time = datetime.now().isoformat(timespec='seconds')
        print(f"Database backed up to disk at {backup_time}")

        # Record timestamp for web dashboard status panel
        try:
            with open(os.path.join(DB_DIR, "last_backup"), 'w') as f:
                f.write(backup_time)
        except OSError as e:
            print(f"WARNING: Could not write last_backup timestamp: {e}")

        # Prune old rows from RAM to prevent unbounded growth on long uptimes
        # retention_days is validated as int at startup — safe to interpolate into SQL modifier
        if not isinstance(retention_days, int) or retention_days <= 0:
            raise ValueError(f"Invalid retention_days: {retention_days!r}")
        cursor = src.cursor()
        cursor.execute(
            "DELETE FROM readings WHERE timestamp < datetime('now', ?);",
            (f"-{retention_days} days",)
        )
        src.commit()

        # Truncate WAL file after pruning to reclaim RAM pages
        src.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        src.commit()

    except Exception as e:
        print(f"WARNING: Disk backup failed (data safe in RAM): {e}")
    finally:
        if src is not None:
            try:
                src.close()
            except Exception:
                pass

def restore_db_from_backup():
    """On boot, copies the last SD backup into the RAM database."""
    os.makedirs(RAM_DB_DIR, exist_ok=True)

    if not os.path.exists(DB_FILE):
        print("No SD backup found. Starting with empty database.")
        return

    print("Restoring database from SD backup into RAM...")
    src = None
    dst = None
    try:
        src = sqlite3.connect(DB_FILE, timeout=10)
        dst = sqlite3.connect(RAM_DB_FILE, timeout=10)
        src.backup(dst)
        print("Database restored successfully.")
    except Exception as e:
        print(f"Restore failed, starting fresh: {e}")
    finally:
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        if dst is not None:
            try:
                dst.close()
            except Exception:
                pass

def backup_loop(interval_seconds, retention_days):
    """Background thread: fires backup_ram_db_to_disk() on the configured interval.

    Also independently checks /data mount validity every MOUNT_CHECK_SECONDS,
    regardless of how long backup_interval_hours is set to. This is what
    catches a lost/phantom /data mount within minutes instead of it going
    unnoticed for an entire multi-day (or multi-month) uptime — see
    check_data_mount_valid() for why that failure mode is otherwise silent.
    """
    MOUNT_CHECK_SECONDS = 300  # 5 minutes — independent of backup_interval_hours
    elapsed_since_backup = 0

    while True:
        time.sleep(MOUNT_CHECK_SECONDS)
        elapsed_since_backup += MOUNT_CHECK_SECONDS
        check_data_mount_valid()

        if elapsed_since_backup >= interval_seconds:
            backup_ram_db_to_disk(retention_days)
            elapsed_since_backup = 0

# ---------------------------------------------------------------------------
# Boot integrity check
# ---------------------------------------------------------------------------

def verify_and_recover_db():
    """Runs PRAGMA integrity_check on the SD backup; quarantines if corrupt."""
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)

    if not os.path.exists(DB_FILE):
        return

    print("Checking SD backup integrity...")
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            result = cursor.fetchone()[0]
        finally:
            conn.close()

        if result.lower() != "ok":
            raise sqlite3.DatabaseError(f"Integrity check failed: {result}")
        print("Database integrity: OK")

    except sqlite3.DatabaseError as e:
        print(f"DATABASE CORRUPTION DETECTED: {e}")
        corrupt_path = f"{DB_FILE}.corrupt.{int(time.time())}"
        shutil.move(DB_FILE, corrupt_path)
        print(f"Quarantined corrupted file to: {corrupt_path}")
        with open(DB_CORRUPT_FLAG, 'w') as f:
            f.write(str(time.time()))

# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

def init_db():
    """Creates the schema in the RAM database and enables WAL mode."""
    conn = sqlite3.connect(RAM_DB_FILE, timeout=10)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     DATETIME DEFAULT CURRENT_TIMESTAMP,
                sensor_name   TEXT,
                temperature_f REAL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON readings(timestamp);")
        conn.commit()
        print("Database schema initialized (WAL mode active).")
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Telemetry insert
# ---------------------------------------------------------------------------

def log_telemetry(ntp_sync_year, heartbeat_url):
    """Reads the IPC file and inserts valid sensor readings into the RAM database."""
    if not os.path.exists(IPC_FILE):
        print("IPC file not found, skipping DB write.")
        return

    try:
        payload = safe_read_json(IPC_FILE)
        if payload is None:
            return

        sensor_data   = payload.get("sensors", {})
        ipc_timestamp = payload.get("timestamp", 0)

        # Reject pre-NTP timestamps
        if time.gmtime(ipc_timestamp).tm_year < ntp_sync_year:
            print("IPC data has pre-NTP timestamp. Skipping write.")
            return

        conn = sqlite3.connect(RAM_DB_FILE, timeout=10)
        try:
            cursor = conn.cursor()
            for sensor_name, temp_f in sensor_data.items():
                if temp_f is not None:
                    cursor.execute(
                        "INSERT INTO readings (sensor_name, temperature_f) VALUES (?, ?)",
                        (sensor_name, temp_f)
                    )
            conn.commit()
            print(f"Logged telemetry at {datetime.now().isoformat()}")
        finally:
            conn.close()

        # Heartbeat ping — only fires on successful write
        if heartbeat_url:
            try:
                urllib.request.urlopen(heartbeat_url, timeout=10)
            except Exception as e:
                print(f"Heartbeat ping failed (non-fatal): {e}")

    except (json.JSONDecodeError, KeyError, sqlite3.Error) as e:
        print(f"Failed to log telemetry: {e}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("Starting Database Logger...")

    config                = load_config()
    POLL_INTERVAL_SECONDS = config.getint('sampling', 'db_commit_interval')
    NTP_SYNC_YEAR         = config.getint('system', 'ntp_sync_year')

    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(RAM_DB_DIR, exist_ok=True)

    # Verify /data is a genuine mountpoint before touching it at all — catches
    # a bad namespace/mount state immediately at boot rather than waiting for
    # the first periodic check in backup_loop().
    check_data_mount_valid()

    # Boot sequence: integrity check and restore happen before NTP gate so
    # historical data is available immediately regardless of network state.
    verify_and_recover_db()
    restore_db_from_backup()
    init_db()
    wait_for_ntp_sync(NTP_SYNC_YEAR, "Database Logger")     # Block writes until clock is valid

    backup_interval = config.getint('database', 'backup_interval_hours') * 3600
    retention_days  = config.getint('database', 'retention_days')
    backup_thread   = threading.Thread(target=backup_loop, args=(backup_interval, retention_days), daemon=True)
    backup_thread.start()

    heartbeat_url = config.get('network', 'heartbeat_url', fallback='')

    # Register SIGTERM handler for clean shutdown — triggers a final SD backup
    # so no data is lost when the service is stopped for reboot or OTA update.
    def _shutdown_handler(signum, frame):
        print("SIGTERM received — running final database backup before exit.")
        backup_ram_db_to_disk(retention_days)
        print("Final backup complete. Exiting.")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown_handler)

    while True:
        loop_start = time.monotonic()
        log_telemetry(NTP_SYNC_YEAR, heartbeat_url)
        elapsed    = time.monotonic() - loop_start
        sleep_time = max(0, POLL_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
