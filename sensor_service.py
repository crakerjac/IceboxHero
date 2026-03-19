"""
Module 2 — Sensor Acquisition Service (sensor_service.py)

Reads DS18B20 temperatures via the Linux 1-Wire kernel interface and writes
current state atomically to the RAM disk IPC file every poll_interval seconds.

Design notes:
  - Dual-read per sensor: first read discarded to flush kernel cache, second read used.
  - 85.0 C filter: power-on reset artifact from the DS18B20, always discarded.
  - ThreadPoolExecutor enforces a per-sensor timeout without blocking main-thread signals.
  - On timeout, raises SystemExit to let systemd restart the service and clear hung threads.
  - Atomic write (write tmp → os.replace) prevents consumers from reading a partial file.
  - Drift-free polling via time.monotonic() compensates for sensor read time.
"""

import os
import time
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from config_helper import load_config

IPC_TEMP_FILE = "/run/iceboxhero/telemetry_state.tmp"
IPC_FILE      = "/run/iceboxhero/telemetry_state.json"
BASE_DIR      = '/sys/bus/w1/devices/'

# max_workers matches max expected sensors so reads run in parallel.
# Timeout raised to 2.75 s to accommodate: ~750 ms first read + 750 ms sleep + ~750 ms second read.
executor = ThreadPoolExecutor(max_workers=4)


def read_temp_raw(device_file):
    """Reads the raw lines from the 1-Wire kernel interface."""
    try:
        with open(device_file, 'r') as f:
            lines = f.readlines()
        return lines
    except OSError:
        return None


def process_sensor(device_folder):
    """Handles the dual-read, value extraction, and 85.0 C filtering."""
    device_file = os.path.join(device_folder, 'w1_slave')

    if not os.path.exists(device_file):
        return None

    # First read: discarded to force a fresh conversion on the 1-Wire bus
    read_temp_raw(device_file)
    time.sleep(0.75)
    # Second read: actual data
    lines = read_temp_raw(device_file)

    if not lines or len(lines) < 2 or lines[0].strip()[-3:] != 'YES':
        return None

    equals_pos = lines[1].find('t=')
    if equals_pos != -1:
        temp_string = lines[1][equals_pos + 2:]
        temp_c = float(temp_string) / 1000.0

        # Power-on reset anomaly filter: DS18B20 returns exactly 85.0 C on startup
        if temp_c == 85.0:
            return None

        temp_f = round((temp_c * 9.0 / 5.0) + 32.0, 1)

        # Sanity bounds: reject physically implausible readings
        if temp_f < -50 or temp_f > 100:
            return None

        return temp_f

    return None


def read_sensor_with_timeout(device_folder, timeout=2.75):
    """Submits a sensor read to the thread pool and enforces a hard timeout."""
    future = executor.submit(process_sensor, device_folder)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        print(f"Timeout reading {device_folder}")
        # Force systemd to restart the service and clear the hung thread pool
        raise SystemExit("Thread pool compromised by sensor timeout. Forcing service restart.")
    except Exception as e:
        print(f"Error reading sensor {device_folder}: {e}")
        return None


def write_ipc_state(sensor_data):
    """Writes the JSON payload atomically to the RAM disk."""
    payload = {
        "timestamp": int(time.time()),
        "monotonic": time.monotonic(),
        "sensors": sensor_data
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

    config = load_config()
    poll_interval      = config.getint('sampling', 'poll_interval')
    configured_sensors = dict(config.items('sensors'))  # {"28-xxxx": "big_freezer", ...}

    # Write an initial all-None boot state so consumers don't crash on missing file
    boot_state = {name: None for name in configured_sensors.values()}
    write_ipc_state(boot_state)


    while True:
        loop_start_time = time.monotonic()
        current_readings = {}

        for rom_id, logical_name in configured_sensors.items():
            device_folder = os.path.join(BASE_DIR, rom_id)

            if os.path.exists(device_folder):
                current_readings[logical_name] = read_sensor_with_timeout(device_folder)
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
