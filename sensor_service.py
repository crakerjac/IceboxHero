"""
Module 2 — Sensor Acquisition Service (sensor_service.py)

Reads DS18B20 temperatures via the Linux 1-Wire kernel interface and writes
current state atomically to the RAM disk IPC file every poll_interval seconds.

Design notes:
  - Single read per sensor: the Linux w1_therm kernel driver blocks the file
    read while issuing the CONVERT_T command and waiting for conversion to
    complete (~750 ms). A second read is unnecessary.
  - All sensor futures are submitted before any result is collected, allowing
    true parallel reads across all sensors.
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

# max_workers matches max expected sensors so all reads run in parallel.
# Timeout is 1.5 s: the kernel blocks for ~750 ms during conversion,
# plus a 750 ms margin for scheduler jitter.
executor = ThreadPoolExecutor(max_workers=4)


def process_sensor(device_folder):
    """Single read from the 1-Wire kernel interface with filtering.

    The w1_therm driver blocks the open() call while performing the CONVERT_T
    command and waiting for the sensor to complete. One read is sufficient.
    """
    device_file = os.path.join(device_folder, 'w1_slave')

    if not os.path.exists(device_file):
        return None

    try:
        with open(device_file, 'r') as f:
            lines = f.readlines()
    except OSError:
        return None

    if not lines or len(lines) < 2 or lines[0].strip()[-3:] != 'YES':
        return None

    equals_pos = lines[1].find('t=')
    if equals_pos == -1:
        return None

    temp_c = float(lines[1][equals_pos + 2:]) / 1000.0

    # Power-on reset anomaly filter: DS18B20 returns exactly 85.0 C on startup
    if temp_c == 85.0:
        return None

    temp_f = round((temp_c * 9.0 / 5.0) + 32.0, 1)

    # Sanity bounds: reject physically implausible readings
    if temp_f < -50 or temp_f > 100:
        return None

    return temp_f


def write_ipc_state(sensor_data):
    """Writes the JSON payload atomically to the RAM disk."""
    payload = {
        "timestamp": int(time.time()),
        "monotonic": time.monotonic(),
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

    config = load_config()
    poll_interval      = config.getint('sampling', 'poll_interval')
    configured_sensors = dict(config.items('sensors'))  # {"28-xxxx": "logical_name", ...}

    # Write an initial all-None boot state so consumers don't crash on missing file
    boot_state = {name: None for name in configured_sensors.values()}
    write_ipc_state(boot_state)

    while True:
        loop_start_time = time.monotonic()

        # Submit all sensor reads in parallel before collecting any results
        futures = {}
        for rom_id, logical_name in configured_sensors.items():
            device_folder = os.path.join(BASE_DIR, rom_id)
            if os.path.exists(device_folder):
                futures[logical_name] = executor.submit(process_sensor, device_folder)
            else:
                futures[logical_name] = None
                print(f"Missing device path for: {rom_id} ({logical_name})")

        # Collect results — timeout per sensor, SystemExit on hung thread pool
        current_readings = {}
        for logical_name, future in futures.items():
            if future is None:
                current_readings[logical_name] = None
                continue
            try:
                current_readings[logical_name] = future.result(timeout=1.5)
            except TimeoutError:
                print(f"Timeout reading sensor: {logical_name}")
                raise SystemExit("Thread pool compromised by sensor timeout. Forcing service restart.")
            except Exception as e:
                print(f"Error reading sensor {logical_name}: {e}")
                current_readings[logical_name] = None

        write_ipc_state(current_readings)

        # Drift-free sleep: subtract actual read time from the configured interval
        elapsed    = time.monotonic() - loop_start_time
        sleep_time = max(0.0, poll_interval - elapsed)
        time.sleep(sleep_time)


if __name__ == '__main__':
    main()
