"""
Module 6 — Web Server & Dashboard (web_server.py)

Serves a local Flask dashboard on port 8080 (configurable).
Two data sources:
  - /api/current  → reads /run/iceboxhero/telemetry_state.json (RAM, updates every 60 s)
  - /api/history  → queries the RAM SQLite database for the last 24 hours

The dashboard JS polls /api/current every 30 seconds and /api/history every
5 minutes. Chart.js must be downloaded and placed at static/chart.min.js —
the dashboard is designed to work without any internet connectivity.
"""

import os
import json
import sqlite3
import time

from flask import Flask, jsonify, render_template
from config_helper import load_config

app = Flask(__name__)

VERSION_FILE = os.path.join(os.path.dirname(__file__), 'VERSION')

def get_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except Exception:
        return 'unknown'

config        = load_config()
IPC_FILE      = "/run/iceboxhero/telemetry_state.json"
DB_FILE       = "/run/icebox_db/freezer_monitor.db"   # Live RAM database
WEB_PORT      = config.getint('network', 'web_port')
TEMP_WARNING  = config.getfloat('sampling', 'temp_warning')
TEMP_CRITICAL = config.getfloat('sampling', 'temp_critical')


def safe_read_json(path, retries=3):
    for _ in range(retries):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)
    return None


def get_current_state():
    """Returns the latest IPC payload from RAM disk."""
    if not os.path.exists(IPC_FILE):
        return {"error": "Booting or IPC file missing", "sensors": {}}

    try:
        payload = safe_read_json(IPC_FILE)
        if payload is None:
            return {"error": "IPC read error", "sensors": {}}
        return payload
    except (json.JSONDecodeError, IOError):
        return {"error": "IPC read error", "sensors": {}}


def get_24h_history():
    """Queries the RAM SQLite database for the last 24 hours of readings."""
    if not os.path.exists(DB_FILE):
        return []

    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp, sensor_name, temperature_f
                FROM readings
                WHERE timestamp >= datetime('now', '-1 day')
                ORDER BY timestamp ASC
            """)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    except sqlite3.Error as e:
        print(f"Database error in get_24h_history: {e}")
        return []


def get_watchdog_status():
    """Returns watchdog active state by checking icebox-watchdog.service."""
    try:
        import subprocess
        result = subprocess.run(
            ['systemctl', 'is-active', 'icebox-watchdog'],
            capture_output=True, text=True, timeout=2
        )
        return result.stdout.strip() == 'active'
    except Exception:
        return None  # Unknown


def get_system_status():
    """Returns system health metrics for the dashboard status panel."""
    import subprocess
    import shutil

    status = {}

    # IPC file age in seconds
    try:
        mtime = os.path.getmtime(IPC_FILE)
        status['ipc_age_seconds'] = int(time.time() - mtime)
    except OSError:
        status['ipc_age_seconds'] = None

    # Last SD backup time — db_logger writes a timestamp file on each backup
    BACKUP_TS_FILE = "/data/db/last_backup"
    try:
        with open(BACKUP_TS_FILE) as f:
            status['last_backup'] = f.read().strip()
    except OSError:
        status['last_backup'] = None

    # /data disk usage
    try:
        usage = shutil.disk_usage('/data')
        status['data_disk_total_gb'] = round(usage.total / 1e9, 1)
        status['data_disk_used_gb']  = round(usage.used  / 1e9, 1)
        status['data_disk_pct']      = round(usage.used  / usage.total * 100, 1)
    except OSError:
        status['data_disk_total_gb'] = None
        status['data_disk_used_gb']  = None
        status['data_disk_pct']      = None

    # System uptime
    try:
        with open('/proc/uptime') as f:
            seconds = float(f.read().split()[0])
        days    = int(seconds // 86400)
        hours   = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        if days > 0:
            status['uptime'] = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            status['uptime'] = f"{hours}h {minutes}m"
        else:
            status['uptime'] = f"{minutes}m"
    except OSError:
        status['uptime'] = None

    # Pi CPU temperature
    try:
        result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True, timeout=2)
        # Output format: temp=42.8'C
        temp_str = result.stdout.strip().replace("temp=", "").replace("'C", "")
        cpu_c = float(temp_str)
        status['cpu_temp_f'] = round(cpu_c * 9/5 + 32, 1)
    except Exception:
        status['cpu_temp_f'] = None

    return status


@app.route('/')
def index():
    """Serves the main dashboard, injecting threshold values from config."""
    return render_template('index.html', warning=TEMP_WARNING, critical=TEMP_CRITICAL, version=get_version())


@app.route('/api/current')
def api_current():
    """Returns current sensor readings from the RAM IPC file."""
    state = get_current_state()
    state['watchdog_active'] = get_watchdog_status()
    return jsonify(state)


@app.route('/api/history')
def api_history():
    """Returns the last 24 hours of database readings."""
    return jsonify(get_24h_history())


@app.route('/api/status')
def api_status():
    """Returns system health metrics for the status panel."""
    return jsonify(get_system_status())


if __name__ == '__main__':
    from waitress import serve
    serve(app, host='0.0.0.0', port=WEB_PORT)
