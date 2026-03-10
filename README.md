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

### Step 1 — Prevent Auto-Expand and Create the /data Partition (one-time, manual)

This is the only step that cannot be automated. `fdisk` is destructive and must be run deliberately.

#### 1a. Disable Auto-Expand Before First Boot

By default, Raspberry Pi OS expands the root partition (`p2`) to fill the entire SD card on first boot. You must prevent this **before the Pi boots for the first time** to leave unallocated space for the `/data` partition (`p3`).

1. Flash Raspberry Pi OS Lite (64-bit recommended) to your SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Remove and re-insert the SD card into your PC. The `bootfs` FAT32 volume will mount automatically.
3. Open `cmdline.txt` in a text editor.
4. Find and **delete** this exact string (leave everything else on the line intact):
   ```
   init=/usr/lib/raspi-config/init_resize.sh
   ```
5. Save the file, eject the SD card, and insert it into the Pi. Boot normally.

> If you skip this step and the root partition has already been expanded to fill the card, you will need to shrink `p2` with a partition tool before you can create `p3`. It is much easier to do this before first boot.

#### 1b. Create the /data Partition

Once booted and logged in, you will have unallocated space after `p2`. Use `fdisk` to create `p3`:

```bash
sudo fdisk /dev/mmcblk0
```

At the `fdisk` prompt:
```
Command: n          # new partition
Type:    p          # primary
Number:  3          # partition number
First sector:       # press Enter to accept default (first available sector)
Last sector:        # press Enter to accept default (rest of the drive)
Command: w          # write and exit
```

Format, mount, and set permissions:

```bash
sudo mkfs.ext4 /dev/mmcblk0p3
sudo mkdir -p /data/config /data/db /data/logs
```

Add to `/etc/fstab` so it mounts automatically on every boot:
```
/dev/mmcblk0p3  /data  ext4  defaults,noatime  0  2
```

```bash
sudo mount -a
sudo chown -R pi:pi /data
```

Verify: `mountpoint /data` should print `/data is a mountpoint`.

### Step 2 — Clone and run setup

```bash
git clone git@github.com:crakerjac/freezerPi.git
cd freezerPi
sudo ./setup.sh
```

`setup.sh` handles everything else automatically:

- Hardware interfaces (`/boot/firmware/config.txt` — watchdog, SPI, 1-Wire)
- System packages and Python dependencies
- Source code deployment to `/opt/freezerpi/`
- Chart.js download for the local dashboard
- Watchdog daemon configuration
- logrotate configuration
- All five systemd services (installed and enabled)
- Weekly CRON job for database maintenance

### Step 3 — Edit config.ini

The script copies the template and then stops so you can fill in your values:

```bash
sudo nano /data/config/config.ini
```

Required changes:

- `[sensors]` — Replace placeholder ROM IDs with your actual DS18B20 addresses. **Reboot first** so the 1-Wire overlay loads, then find them at `/sys/bus/w1/devices/`. Look for entries starting with `28-`.
- `[email]` — Your Gmail address and [App Password](https://myaccount.google.com/apppasswords) (required if 2FA is enabled).
- `[network]` — Your two healthchecks.io ping URLs, or leave as placeholders to disable.

> `config.ini` is excluded from git via `.gitignore`. Your live file with real credentials stays on the Pi and will never be accidentally committed.

> **ST7735S Display Note:** Identify your panel variant by the colored tab on the flex cable ribbon where it meets the PCB, and wire the driver stub in `display_service.py` accordingly:
>
> | Tab Color | Constructor |
> |---|---|
> | Red | `st7735.ST7735R(spi, ...)` |
> | Black | `st7735.ST7735R(spi, ..., bgr=True)` |
> | Green | `st7735.ST7735R(spi, ..., bgr=True)` + possible x/y offsets |
> | 0.96" 80×160 | Add `width=80, height=160, x_offset=26, y_offset=1` |
>
> After wiring, flash a solid red frame. If it renders as blue, add `bgr=True`.

### Step 4 — Reboot, then start services

```bash
sudo reboot
```

After reboot, connect your sensors if not already done, then:
```bash
sudo ./start_services.sh
```

This confirms sensors are detected, starts all five services, then arms the watchdog last. The script prints a live status summary — all five services and the watchdog should show as active (green) before proceeding.

### Step 5 — Enable read-only root filesystem (last)

Only after everything is verified working:

```bash
sudo raspi-config
# Performance Options → Overlay File System → Enable
```

The `/data` partition bypasses the overlay and remains writable. The root partition becomes read-only, protecting the OS from SD card corruption on sudden power loss. Do this step last — it is difficult to make further system changes once enabled.

---

## Web Dashboard

Once services are running, the dashboard is accessible from any browser on your local network.

### Finding Your Pi's Address

The easiest way is by hostname — Raspberry Pi OS advertises itself via mDNS by default:

```
http://freezerpi.local:8080
```

If that doesn't resolve (some Windows networks block mDNS), find the IP address directly on the Pi:

```bash
hostname -I
```

Then navigate to `http://<ip-address>:8080` from any device on the same network.

The port is configurable in `config.ini` under `[network] → web_port`.

### What the Dashboard Shows

- **Current temperatures** — one card per sensor, color-coded to match the physical display (green = normal, yellow = warning, red = critical). Updates every 30 seconds from the RAM disk IPC file.
- **24-hour history graph** — line chart per sensor, pulled directly from the SQLite database. Refreshes every 5 minutes to match the database commit interval.

### Notes

- The dashboard is **read-only** — there are no controls, only monitoring.
- Chart.js is served locally from `/opt/freezerpi/static/chart.min.js` — the dashboard loads instantly and works fully during an internet outage.
- Timestamps on the graph are stored in UTC in SQLite and converted to your browser's local timezone automatically for display.
- The dashboard is served on all network interfaces (`0.0.0.0`). It is intended for use on a trusted private network only — there is no authentication.

---

## Operations

### Starting and Stopping Services

Two helper scripts manage the service lifecycle. The critical rule is that the **watchdog must always be stopped before the FreezerPi services** — stopping a service without stopping the watchdog first means the IPC file stops updating, and the watchdog will force a hardware reboot 180 seconds later.

```bash
sudo ./stop_services.sh     # watchdog first, then all five services
sudo ./start_services.sh    # all five services, then watchdog last
```

`start_services.sh` includes two preflight checks before starting anything:

- Confirms at least one DS18B20 sensor is visible on the 1-Wire bus at `/sys/bus/w1/devices/28-*`
- Confirms `config.ini` no longer contains the placeholder sensor ROM IDs

If either check fails it warns you and prompts before continuing. It also waits 5 seconds after starting the FreezerPi services before starting the watchdog, giving `sensor_service` time to write the initial IPC file.

### Working Without Sensors Connected (Setup and Maintenance Mode)

Any time you need to work on the system without sensors physically connected — editing config, redeploying code, running `uninstall.sh` — stop services first:

```bash
sudo ./stop_services.sh
# do your work
sudo ./start_services.sh    # when sensors are reconnected and ready
```

### Testing the Setup Script

To re-run `setup.sh` from a clean state without reimaging the SD card:

```bash
sudo ./stop_services.sh
sudo ./uninstall.sh
sudo ./setup.sh
```

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
├── setup.sh                     # Automated setup script — run after Step 1
├── uninstall.sh                 # Reverses setup.sh — for testing and reinstallation
├── start_services.sh            # Starts all services and the watchdog
├── stop_services.sh             # Stops watchdog first, then all services
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
├── freezerpi-tmpfiles.conf      # Creates /run/freezerpi and /run/freezer_db at boot
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
