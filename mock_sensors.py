"""
mock_sensors.py — Simulated Sensor Service (Development Tool)

Replaces sensor_service.py for testing without physical DS18B20 sensors.
Reads sensor names and thresholds from /data/config/config.ini and writes
the same IPC file format that all downstream services consume.

Usage:
    python3 mock_sensors.py                 # default: gentle sine wave around 0°F
    python3 mock_sensors.py --mode normal   # steady normal temps
    python3 mock_sensors.py --mode warning  # steady at warning threshold
    python3 mock_sensors.py --mode critical # steady above critical threshold
    python3 mock_sensors.py --mode missing  # first sensor returns None (FAILURE alert)
    python3 mock_sensors.py --mode sine     # slow sine wave drifting through all states
    python3 mock_sensors.py --mode ramp     # ramps up from normal → warning → critical

Stop with Ctrl+C. The IPC file is left in place after exit so downstream
services don't immediately go stale — they will naturally time out after
stale_timeout seconds (default: 600).

NOTE: Stop freezer-sensor.service before running this, or they will race
to write the IPC file:
    sudo systemctl stop freezer-sensor.service
"""

import os
import sys
import json
import time
import math
import argparse
from config_helper import load_config

IPC_FILE = "/run/freezerpi/telemetry_state.json"
IPC_TEMP = "/run/freezerpi/telemetry_state.tmp"


def write_ipc(sensor_data):
    """Atomically writes sensor data to the IPC file.

    Intentionally omits the 'monotonic' field — that field is only valid
    within the long-running sensor_service.py process. Including a monotonic
    value from a short-lived mock process would cause alert_service to
    falsely trigger SYSTEM_FREEZE detection on every run.
    """
    payload = {
        "timestamp": int(time.time()),
        "sensors": sensor_data,
    }
    try:
        with open(IPC_TEMP, 'w') as f:
            json.dump(payload, f)
        os.replace(IPC_TEMP, IPC_FILE)
    except Exception as e:
        print(f"[ERROR] Failed to write IPC: {e}")


def format_display(sensor_data):
    """Formats sensor data for console output."""
    parts = []
    for name, temp in sensor_data.items():
        parts.append(f"{name}: {'--.-F' if temp is None else f'{temp:.1f}F'}")
    return "  |  ".join(parts)


def main():
    parser = argparse.ArgumentParser(description="FreezerPi mock sensor service")
    parser.add_argument(
        '--mode',
        choices=['normal', 'warning', 'critical', 'missing', 'sine', 'ramp'],
        default='sine',
        help='Simulation mode (default: sine)'
    )
    parser.add_argument(
        '--interval',
        type=float,
        default=None,
        metavar='SECONDS',
        help='Override poll interval in seconds (default: uses poll_interval from config.ini)'
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        print("       Edit /data/config/config.ini before running mock_sensors.py")
        sys.exit(1)

    # Warn if freezer-sensor.service is running — it will race on the IPC file
    try:
        import subprocess
        result = subprocess.run(
            ['systemctl', 'is-active', 'freezer-sensor.service'],
            capture_output=True, text=True
        )
        if result.stdout.strip() == 'active':
            print("\n[WARNING] freezer-sensor.service is currently running.")
            print("          It will race with mock_sensors.py to write the IPC file.")
            print("          Stop it first with:")
            print("            sudo systemctl stop freezer-sensor.service")
            print("")
            response = input("Continue anyway? [y/N] ").strip().lower()
            if response != 'y':
                print("Aborted.")
                sys.exit(0)
    except Exception:
        pass  # If systemctl isn't available, skip the check silently

    # Read sensor names and thresholds from config
    sensor_names  = list(dict(config.items('sensors')).values())
    poll_interval = args.interval if args.interval is not None else config.getint('sampling', 'poll_interval')
    temp_warning  = config.getfloat('sampling', 'temp_warning')
    temp_critical = config.getfloat('sampling', 'temp_critical')

    # Base temperatures per mode
    if args.mode == 'normal':
        base_temps = {name: round(temp_warning - 8.0, 1) for name in sensor_names}
    elif args.mode == 'warning':
        base_temps = {name: round(temp_warning + 1.0, 1) for name in sensor_names}
    elif args.mode == 'critical':
        base_temps = {name: round(temp_critical + 2.0, 1) for name in sensor_names}
    elif args.mode == 'missing':
        base_temps = {sensor_names[0]: None}
        if len(sensor_names) > 1:
            base_temps[sensor_names[1]] = round(temp_warning - 8.0, 1)
    else:
        # sine and ramp both start at a normal base
        base_temps = {name: round(temp_warning - 8.0, 1) for name in sensor_names}

    interval_source = f"{poll_interval}s (--interval override)" if args.interval is not None else f"{poll_interval}s (from config.ini)"
    print(f"\nFreezerPi Mock Sensor Service")
    print(f"Mode:          {args.mode}")
    print(f"Sensors:       {', '.join(sensor_names)}")
    print(f"Thresholds:    warning={temp_warning}F  critical={temp_critical}F")
    print(f"Poll interval: {interval_source}")
    print(f"IPC file:      {IPC_FILE}")
    print(f"\nStop with Ctrl+C\n")

    t = 0
    ramp_temp = list(base_temps.values())[0] if args.mode == 'ramp' else 0

    try:
        while True:
            loop_start = time.monotonic()

            if args.mode == 'missing':
                sensor_data = dict(base_temps)

            elif args.mode == 'sine':
                # Gentle sine wave: drifts from 8°F below warning up through critical
                amplitude = (temp_critical - temp_warning) + 10.0
                sensor_data = {}
                for i, name in enumerate(sensor_names):
                    # Offset each sensor slightly so they don't move in lockstep
                    phase = (t / 60.0) + (i * math.pi / 4)
                    temp  = round(base_temps[name] + math.sin(phase) * amplitude, 1)
                    sensor_data[name] = temp

            elif args.mode == 'ramp':
                # Ramps up 1°F every poll cycle, wraps back to base after critical+5
                sensor_data = {name: round(ramp_temp, 1) for name in sensor_names}
                ramp_temp += 1.0
                if ramp_temp > temp_critical + 5:
                    ramp_temp = list(base_temps.values())[0]
                    print("[RAMP] Wrapped back to base temperature")

            else:
                # normal, warning, critical — steady values with tiny noise
                sensor_data = {}
                for name, base in base_temps.items():
                    if base is None:
                        sensor_data[name] = None
                    else:
                        noise = math.sin(t * 0.3) * 0.2
                        sensor_data[name] = round(base + noise, 1)

            # Annotate each reading with its alert state for console clarity
            annotations = []
            for name, temp in sensor_data.items():
                if temp is None:
                    annotations.append(f"{name}=MISSING")
                elif temp >= temp_critical:
                    annotations.append(f"{name}={temp}F [CRITICAL]")
                elif temp >= temp_warning:
                    annotations.append(f"{name}={temp}F [WARNING]")
                else:
                    annotations.append(f"{name}={temp}F [normal]")

            write_ipc(sensor_data)
            print(f"[{time.strftime('%H:%M:%S')}]  {'  |  '.join(annotations)}")

            elapsed    = time.monotonic() - loop_start
            sleep_time = max(0.0, poll_interval - elapsed)
            t += poll_interval
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\nMock sensor stopped. IPC file left in place.")
        print(f"Downstream services will go stale after {config.getint('display', 'stale_timeout')}s.")
        if poll_interval > config.getint('display', 'stale_timeout'):
            print(f"WARNING: poll interval ({poll_interval}s) exceeds stale_timeout — services may already be stale.")


if __name__ == '__main__':
    main()
