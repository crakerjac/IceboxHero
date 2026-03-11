"""
Module 6 — Web Server & Dashboard (web_server.py)

Serves a local Flask dashboard on port 8080 (configurable).
Two data sources:
  - /api/current  → reads /run/freezerpi/telemetry_state.json (RAM, updates every 60 s)
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
IPC_FILE      = "/run/freezerpi/telemetry_state.json"
DB_FILE       = "/run/freezer_db/freezer_monitor.db"   # Live RAM database
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
    """Returns watchdog active state by checking systemd."""
    try:
        import subprocess
        result = subprocess.run(
            ['systemctl', 'is-active', 'watchdog'],
            capture_output=True, text=True, timeout=2
        )
        return result.stdout.strip() == 'active'
    except Exception:
        return None  # Unknown


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


if __name__ == '__main__':
    from waitress import serve
    serve(app, host='0.0.0.0', port=WEB_PORT)
