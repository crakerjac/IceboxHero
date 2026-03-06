# FreezerPi — Raspberry Pi Freezer Monitor

A self-contained, fault-tolerant freezer temperature monitoring system built on a Raspberry Pi Zero 2 W. All acquisition, storage, alerting, web hosting, and watchdog functions run locally with no external backend dependency. Designed for unattended, always-on operation with aggressive SD card wear minimization and hardware-enforced auto-recovery.

---

## Features

- Continuous DS18B20 temperature monitoring via 1-Wire bus
- Local ST7735S LCD display with color-coded status and 1 Hz critical flashing
- Piezo buzzer alarm with hardware silence button
- Email alerts (Gmail/SMTP) with in-memory retry queue — survives network outages
- External uptime monitoring via [healthchecks.io](https://healthchecks.io) dead-man's snitch
- SQLite database lives in RAM; backs up to SD card every 4 hours
- Read-only root filesystem — SD card protected against power-loss corruption
- Hardware watchdog forces a reboot if the sensor service hangs
- Flask web dashboard with 24-hour temperature graph, served entirely from local storage
- All behavior tunable via a single `config.ini` — no code changes required

---

## Hardware Requirements

| Component | Details |
|---|---|
| Compute | Raspberry Pi Zero 2 W |
| Sensors | DS18B20 digital temperature sensors, 1-Wire bus (GPIO4), 4.7 kΩ pull-up resistor |
| Display | ST7735S 1.8" SPI LCD |
| Buzzer | Active HIGH piezo buzzer (GPIO17) |
| Silence Button | Momentary push button, active LOW (GPIO27) |

### Default GPIO Pinout

| Signal | GPIO | Physical Pin |
|---|---|---|
| 1-Wire Data | GPIO4 | Pin 7 |
| Buzzer | GPIO17 | Pin 11 |
| Silence Button | GPIO27 | Pin 13 |
| LCD DC | GPIO24 | Pin 18 |
| LCD RST | GPIO25 | Pin 22 |
| SPI MOSI | GPIO10 | Pin 19 |
| SPI CLK | GPIO11 | Pin 23 |
| SPI CE0 (LCD CS) | GPIO8 | Pin 24 |

All pins are configurable in `config.ini`.

---

## System Architecture

Six independent software modules communicate exclusively through shared files on the RAM disk (`/run`). No module calls another directly — a crash in any single module does not affect the others. systemd restarts each module independently.

```
DS18B20 Sensors
      │
      ▼
┌──────────────────┐    atomic write    ┌───────────────────────────┐
│  sensor_service  │ ─────────────────▶ │  /run/telemetry_state     │
│   (Module 2)     │                    │         .json             │
└──────────────────┘                    └─────────────┬─────────────┘
                                                      │  reads
                           ┌──────────────────────────┼──────────────────────────┐
                           ▼                          ▼                          ▼
               ┌───────────────────┐    ┌──────────────────┐    ┌───────────────────┐
               │  display_service  │    │  alert_service   │    │    db_logger      │
               │   (Module 3)      │    │   (Module 4)     │    │   (Module 5)      │
               └───────────────────┘    └──────────────────┘    └───────────────────┘
                      │                        │                         │
               ST7735S LCD              Buzzer / Email            RAM SQLite DB
                                                                  /run/freezer_db/
                                                                         │
                                                                  4-hr SD backup
                                                                  /data/db/
                                                                         │
                                                                  web_server (Module 6)
                                                                  Flask dashboard :8080
```

### Module Summary

| Module | File(s) | Role |
|---|---|---|
| 0 — Configuration | `config_helper.py`, `config.ini` | Shared config parser; all tunable parameters |
| 1 — OS & Services | `systemd/*.service`, `watchdog.conf` | Filesystem layout, watchdog, systemd units |
| 2 — Sensor Acquisition | `sensor_service.py` | DS18B20 1-Wire reads; atomic IPC file writer |
| 3 — Display | `display_service.py` | ST7735S LCD driver; color-coded status rendering |
| 4 — Alerts & Email | `alert_service.py` | Buzzer control, GPIO interrupt, SMTP retry queue |
| 5 — Database Logger | `db_logger.py`, `db_maintenance.py` | RAM SQLite DB; 4-hour SD backup; weekly pruning |
| 6 — Web Server | `web_server.py`, `templates/index.html` | Flask REST API; 24-hour graph dashboard |

---

## Filesystem Design

Three storage areas with distinct access patterns:

| Path | Type | Purpose |
|---|---|---|
| `/opt/freezerpi/` | Read-Only (overlay) | All Python source code |
| `/run/` | RAM (tmpfs) | IPC state file; live SQLite database |
| `/data/` | Read-Write (ext4) | SD backup of SQLite; config; maintenance logs |

**SD card writes under normal operation:**
- One full database backup every 4 hours (configurable)
- Weekly CRON maintenance log
- Zero writes from the OS root partition

The live database resides entirely in RAM (`/run/freezer_db/freezer_monitor.db`). On each boot it is restored from the last SD card backup. On a sudden power loss, up to 4 hours of temperature history may be lost — this is intentional. The SD card's longevity is the design priority.

---

## Operational Behavior

### Temperature State Machine

| State | Condition | LCD | Buzzer | Email |
|---|---|---|---|---|
| Normal | < 10 °F | White on Black | Off | — |
| Warning | ≥ 10 °F | Black on Yellow | Off | Yes (60-min cooldown) |
| Critical | ≥ 15 °F, 2 consecutive reads | Flashing White/Red @ 1 Hz | On | Yes (60-min cooldown) |
| Missing Sensor | Read timeout or failure | `--.-F`, flashing red | On | Yes |
| Stale Data | IPC file > 10 min old | `STALE DATA`, flashing | On | Yes |

All thresholds are configurable in `config.ini`.

### Email Alerts

Emails arrive with one of two subject prefixes to support inbox filtering:

- **`[ALERT]`** — Requires immediate attention. Covers: CRITICAL, WARNING, FAILURE, SYSTEM_FREEZE, SYSTEM_ERROR.
- **`[STATUS]`** — Informational only. Covers: SYSTEM_BOOT.

**Recommended Gmail filter:** Subject contains `[STATUS] Freezer Monitor` → Skip Inbox, Mark as read, Apply label.

The email thread runs independently of the buzzer. If the network is down at alert time, the email is queued in memory and retried every 5 minutes until it succeeds.

### Silence Button

Pressing the button silences the buzzer for 1 hour. The alarm condition continues to be tracked in software. If the temperature remains critical after 1 hour, the buzzer reactivates automatically.

### Hardware Watchdog

The Linux hardware watchdog monitors `/run/telemetry_state.json` for changes. If `sensor_service.py` fails to update the file for 180 consecutive seconds, the watchdog forces a full hardware reboot. systemd restarts all services automatically on reboot. The 180-second window accommodates the 60-second polling interval plus sensor conversion time and scheduler jitter.

### External Health Monitoring (healthchecks.io)

Two independent UUIDs provide visibility into different failure modes:

- **System-alive ping** — Fired after every successful database write (every 5 min). Grace: 15 min. Detects Pi death, power loss, or DB loop crash.
- **Email-alive ping** — Fired after every successful email send. Grace: 25 hours. Detects Gmail credential expiration or SMTP API changes — independent of whether your own inbox is working.

Both URLs are optional. Leave them as the placeholder value in `config.ini` to disable.

---

## Installation

### 1. Prepare the SD Card

Create a three-partition layout:

```
p1  /boot    FAT32   (existing)
p2  /        ext4    (existing — will become read-only after setup)
p3  /data    ext4    (new)
```

```bash
sudo fdisk /dev/mmcblk0          # create p3
sudo mkfs.ext4 /dev/mmcblk0p3
sudo mkdir -p /data/config /data/db /data/logs
```

Add to `/etc/fstab`:
```
/dev/mmcblk0p3  /data  ext4  defaults,noatime  0  2
```

```bash
sudo mount -a
sudo chown -R pi:pi /data
```

### 2. Enable Hardware Interfaces

Add to `/boot/firmware/config.txt` (use `/boot/config.txt` on older OS versions):
```ini
dtparam=watchdog=on
dtparam=spi=on
dtoverlay=w1-gpio,gpiopin=4
```

Reboot after making these changes.

### 3. Install System Packages

```bash
sudo apt update
sudo apt install watchdog python3-pip python3-venv
```

Configure the watchdog daemon. Edit `/etc/watchdog.conf` and add/uncomment:
```ini
watchdog-device = /dev/watchdog
watchdog-timeout = 15
max-load-1 = 24
file = /run/telemetry_state.json
change = 180
```

```bash
sudo systemctl enable --now watchdog
```

Configure log rotation. Create `/etc/logrotate.d/freezerpi`:
```
/data/logs/db_maintenance.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
```

### 4. Deploy Source Code

```bash
sudo mkdir -p /opt/freezerpi/static /opt/freezerpi/templates
sudo cp *.py /opt/freezerpi/
sudo cp templates/index.html /opt/freezerpi/templates/
sudo chown -R pi:pi /opt/freezerpi
```

### 5. Install Python Dependencies

```bash
pip install gpiozero Pillow flask waitress
pip install adafruit-blinka adafruit-circuitpython-rgb-display
```

> **ST7735S Display Note:** The Adafruit library supports multiple ST7735 panel variants with different init sequences. Identify yours by the colored tab on the flex cable ribbon where it meets the PCB:
>
> | Tab Color | Constructor |
> |---|---|
> | Red | `st7735.ST7735R(spi, ...)` |
> | Black | `st7735.ST7735R(spi, ..., bgr=True)` |
> | Green | `st7735.ST7735R(spi, ..., bgr=True)` + possible x/y offsets |
> | 0.96" 80×160 | Add `width=80, height=160, x_offset=26, y_offset=1` |
>
> After wiring, flash a solid red frame. If it shows as blue, add `bgr=True`. Reference: [Adafruit ST7735 driver source](https://github.com/adafruit/Adafruit_CircuitPython_RGB_Display/blob/main/adafruit_rgb_display/st7735.py)

### 6. Configure the System

The repo contains `config.ini.template` with placeholder values. Copy it to its runtime location and edit:

```bash
cp config.ini.template /data/config/config.ini
nano /data/config/config.ini
```

> `config.ini` is excluded from git via `.gitignore`. Your live file with real credentials stays local and will never be accidentally committed. Always edit `/data/config/config.ini` — never the template.

Required edits:

- `[sensors]` — Replace placeholder ROM IDs with your actual DS18B20 addresses. Find them at `/sys/bus/w1/devices/` after wiring with the overlay enabled.
- `[email]` — Set your Gmail address and [App Password](https://myaccount.google.com/apppasswords) (required if 2FA is enabled).
- `[network]` — Set your two healthchecks.io UUIDs, or leave as placeholders to disable.

### 7. Install Chart.js Locally

The dashboard requires Chart.js served from local storage — it must work when the internet is down.

```bash
# Download chart.min.js from https://github.com/chartjs/Chart.js/releases
# Place it at:
/opt/freezerpi/static/chart.min.js
```

### 8. Install and Enable systemd Services

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable freezer-sensor freezer-display freezer-alert freezer-db freezer-web
sudo systemctl start  freezer-sensor freezer-display freezer-alert freezer-db freezer-web
```

### 9. Schedule Weekly Database Maintenance

```bash
crontab -e
# Add the following line:
0 3 * * 0 /usr/bin/python3 /opt/freezerpi/db_maintenance.py >> /data/logs/db_maintenance.log 2>&1
```

### 10. Enable Read-Only Root Filesystem (Final Step)

Only do this after everything is fully tested and running:

```bash
sudo raspi-config
# Navigate to: Performance Options → Overlay File System → Enable
```

The `/data` partition defined in `/etc/fstab` bypasses the overlay and remains writable. The root partition becomes read-only, protecting the OS from SD card corruption on sudden power loss.

---

## Diagnostics

```bash
# Live log stream from any service
journalctl -u freezer-sensor.service -f
journalctl -u freezer-alert.service -b     # current boot only
journalctl -u freezer-db.service -n 50     # last 50 lines

# Check current sensor readings
cat /run/telemetry_state.json

# Check RAM disk usage
df -h /run

# Check all service status at a glance
systemctl status 'freezer-*'
```

---

## Repository Structure

```
freezerpi/
├── README.md
├── LICENSE
├── .gitignore
├── config.ini.template          # Configuration template — copy to /data/config/config.ini
├── config_helper.py             # Shared config parser          (Module 0)
├── sensor_service.py            # DS18B20 acquisition service   (Module 2)
├── display_service.py           # ST7735S LCD display service   (Module 3)
├── alert_service.py             # Buzzer, button, email alerts  (Module 4)
├── db_logger.py                 # RAM SQLite DB + SD backup     (Module 5)
├── db_maintenance.py            # Weekly CRON pruning script    (Module 5)
├── web_server.py                # Flask API and dashboard       (Module 6)
├── templates/
│   └── index.html               # Web dashboard UI
└── systemd/
    ├── freezer-sensor.service
    ├── freezer-display.service
    ├── freezer-alert.service
    ├── freezer-db.service
    └── freezer-web.service
```

---

## License

GNU General Public License v3.0
