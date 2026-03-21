"""
Module 2 — Sensor Acquisition Service (sensor_service.py)

Reads DS18B20 temperatures via the Linux 1-Wire kernel interface and writes
current state atomically to the RAM disk IPC file every poll_interval seconds.

Design notes:
  - Dual read per sensor: first read triggers conversion, second read (after
    750 ms sleep) gets the settled result. Prevents stale reads on a busy bus
    with multiple sensors.
  - Sensors are read sequentially — the 1-Wire bus is single-wire and cannot
    support concurrent reads. Parallel threads cause bus contention.
  - 85.0 C filter: power-on reset artifact from the DS18B20, always discarded.
  - Atomic write (write tmp → os.replace) prevents consumers from reading a partial file.
  - Drift-free polling via time.monotonic() compensates for sensor read time.
"""

import os
import time
import json

from config_helper import load_config, get_sensor_configs

IPC_TEMP_FILE = "/run/iceboxhero/telemetry_state.tmp"
IPC_FILE      = "/run/iceboxhero/telemetry_state.json"
BASE_DIR      = '/sys/bus/w1/devices/'


def read_w1_slave(device_file):
    """Read raw lines from the 1-Wire kernel interface."""
    try:
        with open(device_file, 'r') as f:
            return f.readlines()
    except OSError:
        return None


def parse_temp(lines):
    """Parse temperature from w1_slave lines. Returns °F or None."""
    if not lines or len(lines) < 2 or lines[0].strip()[-3:] != 'YES':
        return None

    equals_pos = lines[1].find('t=')
    if equals_pos == -1:
        return None

    try:
        temp_c = float(lines[1][equals_pos + 2:]) / 1000.0
    except ValueError:
        return None

    # Power-on reset anomaly filter: DS18B20 returns exactly 85.0 C on startup
    if temp_c == 85.0:
        return None

    temp_f = round((temp_c * 9.0 / 5.0) + 32.0, 1)

    # Sanity bounds: reject physically implausible readings
    if temp_f < -50 or temp_f > 100:
        return None

    return temp_f


def process_sensor(device_folder):
    """Dual read from the 1-Wire kernel interface with filtering.

    Although the w1_therm driver blocks during CONVERT_T, a second read
    ensures a fully settled conversion — the first read may catch a bus
    that is still stabilizing from a previous operation, especially under
    load or with multiple sensors on the same bus. The 750ms sleep between
    reads matches the DS18B20 maximum conversion time at 12-bit resolution.
    """
    device_file = os.path.join(device_folder, 'w1_slave')

    if not os.path.exists(device_file):
        return None

    # First read: triggers conversion, result may be from a previous cycle
    read_w1_slave(device_file)
    time.sleep(0.75)
    # Second read: fresh conversion result
    lines = read_w1_slave(device_file)

    return parse_temp(lines)


def write_ipc_state(sensor_data):
    """Writes the JSON payload atomically to the RAM disk."""
    payload = {
        "timestamp": int(time.time()),
        "sensors":   sensor_data
    }

    try:
        with open(IPC_TEMP_FILE, 'w') as f:
            json.dump(payload, f)
            f.flush()
        # Atomic replace: consumers never see a partial file
        os.replace(IPC_TEMP_FILE, IPC_FILE)
    except Exception as e:
        print(f"Failed to write IPC state: {e}")


def main():
    print("Starting Sensor Acquisition Service...")

    config         = load_config()
    poll_interval  = config.getint('sampling', 'poll_interval')
    sensor_configs = get_sensor_configs(config)  # [{id, name, warning, critical}, ...]

    # Write an initial all-None boot state so consumers don't crash on missing file
    boot_state = {s['name']: None for s in sensor_configs}
    write_ipc_state(boot_state)

    while True:
        loop_start_time  = time.monotonic()
        current_readings = {}

        # Read sensors sequentially — the 1-Wire bus is a single-wire protocol
        # and cannot support concurrent reads. Parallel threads cause bus
        # contention and intermittent timeouts.
        for sensor in sensor_configs:
            rom_id        = sensor['id']
            logical_name  = sensor['name']
            device_folder = os.path.join(BASE_DIR, rom_id)
            if os.path.exists(device_folder):
                current_readings[logical_name] = process_sensor(device_folder)
            else:
                current_readings[logical_name] = None
                print(f"Missing device path for: {rom_id} ({logical_name})")

        write_ipc_state(current_readings)

        # Drift-free sleep: subtract actual read time from the configured interval
        elapsed    = time.monotonic() - loop_start_time
        sleep_time = max(0.0, poll_interval - elapsed)
        time.sleep(sleep_time)

if __name__ == '__main__':
    main()
