"""
Module 5 — Database Maintenance (db_maintenance.py)

Run weekly via CRON to prune old rows from the SD card backup and reclaim
disk space with VACUUM. Operates on the SD copy only — the RAM database is
pruned automatically by db_logger.py during each 4-hour backup cycle.

Schedule (add to crontab with: crontab -e):
    0 3 * * 0 /usr/bin/python3 /opt/freezerpi/db_maintenance.py >> /data/logs/db_maintenance.log 2>&1
"""

import sqlite3
import os
from config_helper import load_config

DB_FILE = "/data/db/freezer_monitor.db"   # Intentionally targets the SD backup copy


def prune_and_vacuum():
    if not os.path.exists(DB_FILE):
        print("SD backup database not found. Nothing to prune.")
        return

    try:
        config         = load_config()
        retention_days = config.getint('database', 'retention_days')

        conn = sqlite3.connect(DB_FILE, timeout=10)
        try:
            cursor = conn.cursor()
            print(f"Pruning records older than {retention_days} days...")
            cursor.execute(
                f"DELETE FROM readings WHERE timestamp < datetime('now', '-{retention_days} days');"
            )
            deleted_rows = cursor.rowcount
            conn.commit()
            print(f"Deleted {deleted_rows} old records. Running VACUUM...")
            cursor.execute("VACUUM;")
            conn.commit()
            print("Database maintenance complete.")
        finally:
            conn.close()

    except Exception as e:
        print(f"Database maintenance failed: {e}")


if __name__ == '__main__':
    prune_and_vacuum()
