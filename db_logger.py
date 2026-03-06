"""
Module 5 — Database Logger (db_logger.py)

Reads /run/telemetry_state.json every 5 minutes and inserts valid readings
into a SQLite database that lives entirely in RAM (/run/freezer_db/).

SD card write strategy:
  - All INSERT operations go to the RAM database (zero SD wear).
  - A background thread backs up the RAM database to /data/db/ every 4 hours
    using SQLite's online backup API (atomic, no locking required).
  - On each backup, old rows beyond retention_days are pruned from RAM and
    the WAL file is truncated to reclaim memory.
  - On boot, the last SD backup is restored into RAM before the main loop starts.

Integrity / NTP gates:
  - On boot, PRAGMA integrity_check runs against the SD backup; corruption
    triggers rename-to-.corrupt and sets /run/db_corrupted.flag for alert_service.
  - Writes are blocked until the system clock year >= ntp_sync_year to prevent
    1970-epoch timestamps being written to the database.
  - A heartbeat ping fires after each successful 5-minute write to healthchecks.io.
"""

import os
import json
import time
import sqlite3
import shutil
import threading
import urllib.request
from datetime import datetime
from config_helper import load_config

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

config = load_config()

DB_DIR          = "/data/db"
DB_FILE         = os.path.join(DB_DIR, "freezer_monitor.db")        # SD card backup
RAM_DB_DIR      = "/run/freezer_db"
RAM_DB_FILE     = os.path.join(RAM_DB_DIR, "freezer_monitor.db")    # Live runtime DB
IPC_FILE        = "/run/telemetry_state.json"
DB_CORRUPT_FLAG = "/run/db_corrupted.flag"

POLL_INTERVAL_SECONDS = config.getint('sampling', 'db_commit_interval')
NTP_SYNC_YEAR         = config.getint('system', 'ntp_sync_year')


# ---------------------------------------------------------------------------
# RAM ↔ SD backup
# ---------------------------------------------------------------------------

def backup_ram_db_to_disk():
    """Atomically copies the live RAM database to the SD card, then prunes RAM."""
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        src = sqlite3.connect(RAM_DB_FILE, timeout=10)
        dst = sqlite3.connect(DB_FILE + ".tmp", timeout=10)
        try:
            src.backup(dst)
        finally:
            dst.close()

        os.replace(DB_FILE + ".tmp", DB_FILE)
        print(f"Database backed up to disk at {datetime.now().isoformat()}")

        # Prune old rows from RAM to prevent unbounded growth on long uptimes
        retention_days = config.getint('database', 'retention_days')
        cursor = src.cursor()
        cursor.execute(
            f"DELETE FROM readings WHERE timestamp < datetime('now', '-{retention_days} days');"
        )
        src.commit()

        # Truncate WAL file after pruning to reclaim RAM pages
        src.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        src.commit()

    except Exception as e:
        print(f"WARNING: Disk backup failed (data safe in RAM): {e}")
    finally:
        try:
            src.close()
        except Exception:
            pass


def restore_db_from_backup():
    """On boot, copies the last SD backup into the RAM database."""
    os.makedirs(RAM_DB_DIR, exist_ok=True)

    if os.path.exists(DB_FILE):
        print("Restoring database from SD backup into RAM...")
        try:
            src = sqlite3.connect(DB_FILE, timeout=10)
            dst = sqlite3.connect(RAM_DB_FILE, timeout=10)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
            print("Database restored successfully.")
        except Exception as e:
            print(f"Restore failed, starting fresh: {e}")
    else:
        print("No SD backup found. Starting with empty database.")


def backup_loop(interval_seconds):
    """Background thread: fires backup_ram_db_to_disk() on the configured interval."""
    while True:
        time.sleep(interval_seconds)
        backup_ram_db_to_disk()


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
# NTP gate
# ---------------------------------------------------------------------------

def wait_for_ntp_sync():
    """Blocks until the system clock year reaches ntp_sync_year."""
    print("Checking system clock synchronization for Database Logger...")
    while time.gmtime().tm_year < NTP_SYNC_YEAR:
        print("Clock unsynced. Halting database writes until NTP resolves...")
        time.sleep(5)
    print("Clock synchronized. Database logging authorized.")


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
# Telemetry insert
# ---------------------------------------------------------------------------

def log_telemetry():
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
        if time.gmtime(ipc_timestamp).tm_year < NTP_SYNC_YEAR:
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
        heartbeat = config.get('network', 'heartbeat_url', fallback='')
        if heartbeat:
            try:
                urllib.request.urlopen(heartbeat, timeout=10)
            except Exception as e:
                print(f"Heartbeat ping failed (non-fatal): {e}")

    except (json.JSONDecodeError, KeyError, sqlite3.Error) as e:
        print(f"Failed to log telemetry: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("Starting Database Logger...")

    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(RAM_DB_DIR, exist_ok=True)

    verify_and_recover_db()      # Check SD backup integrity before restoring
    restore_db_from_backup()     # Load SD backup into RAM
    wait_for_ntp_sync()          # Block until clock is valid
    init_db()                    # Create schema in RAM DB (idempotent)

    backup_interval = config.getint('database', 'backup_interval_hours') * 3600
    backup_thread   = threading.Thread(target=backup_loop, args=(backup_interval,), daemon=True)
    backup_thread.start()

    while True:
        loop_start = time.monotonic()
        log_telemetry()
        elapsed    = time.monotonic() - loop_start
        sleep_time = max(0, POLL_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
