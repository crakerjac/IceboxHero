# IceboxHero — Raspberry Pi Zero Freezer Monitor

<p align="center"><img src="static/color_logo.png" width="200" alt="IceboxHero logo"></p>

A self-contained, fault-tolerant freezer temperature monitoring system built on a Raspberry Pi Zero 2 W. All acquisition, storage, alerting, web hosting, and watchdog functions run locally. No cloud backend is required for core operation. Designed for unattended, always-on operation with aggressive SD card wear minimization and hardware-enforced auto-recovery.

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
| LCD BLK (backlight) | GPIO18 | Pin 12 |
| SPI MOSI (LCD SDA) | GPIO10 | Pin 19 |
| SPI CLK (LCD SCL) | GPIO11 | Pin 23 |
| SPI CE0 (LCD CS) | GPIO8 | Pin 24 |

> **Note on display labels:** Chinese ST7735 modules label SPI MOSI as `SDA` and SPI CLK as `SCL`. This is a mislabeling — it is SPI, not I2C. Wire SDA→GPIO10 and SCL→GPIO11. The `BLK` pin controls the backlight; wire it to GPIO18 for software control or to 3.3V for always-on.

All pins are configurable in `config.ini`.

---

## System Architecture

Eight modules make up the full system. Module 0 (`config_helper.py`) is a shared library imported by all services — it is not a running process. Module 1 covers the OS layer: systemd units, tmpfiles.d, watchdog config, and the data mount. Modules 2–6 are independent long-running services that communicate exclusively through shared files on the RAM disk (`/run`). No service calls another directly — a crash in any single service does not affect the others. systemd restarts each service independently.

<p align="center"><img src="docs/architecture-overview.png" alt="System Architecture Diagram"></p>

> Modules 0 and 1 are cross-cutting infrastructure — `config_helper.py` is imported by every service; the systemd units and watchdog underpin all six. They are intentionally omitted from the data-flow diagram above. For full design notes see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

### Module Summary

| Module | File(s) | Role |
|---|---|---|
| 0 — Configuration | `config_helper.py`, `config.ini` | Shared config parser; all tunable parameters |
| 1 — OS & Services | `systemd/*.service`, `watchdog.conf` | Filesystem layout, watchdog, systemd units |
| 2 — Sensor Acquisition | `sensor_service.py` | DS18B20 1-Wire reads; atomic IPC file writer |
| 3 — Display | `display_service.py` | ST7735S LCD driver; color-coded status rendering |
| 4 — Alerts & Email | `alert_service.py` | Buzzer control, GPIO interrupt, SMTP retry queue |
| 5 — Database Logger | `db_logger.py` | RAM SQLite DB; 4-hour SD backup; automatic pruning on each backup cycle |
| 6 — Web Server | `web_server.py`, `templates/index.html` | Flask REST API; 24-hour graph dashboard |


---

## Filesystem Design

Three storage areas with distinct access patterns:

| Path | Type | Purpose |
|---|---|---|
| `/opt/iceboxhero/` | Read-Only (overlay) | All Python source code |
| `/run/` | RAM (tmpfs) | IPC state file; live SQLite database |
| `/data/` | Read-Write (ext4) | SD backup of SQLite; config; maintenance logs |

**SD card writes under normal operation:**
- One full database backup every 4 hours (configurable)
- Zero writes from the OS root partition

The live database resides entirely in RAM (`/run/icebox_db/freezer_monitor.db`). On each boot it is restored from the last SD card backup. On a sudden power loss, up to 4 hours of temperature history may be lost — this is intentional. The SD card's longevity is the design priority.

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

> **Note:** The hardware watchdog reboots the Pi after 180 seconds of stale IPC data — well before the 10-minute STALE DATA display threshold is reached. The STALE DATA state is a second line of defence for the unlikely scenario where the watchdog daemon itself fails.

### Email Alerts

Emails arrive with one of two subject prefixes to support inbox filtering:

- **`[ALERT]`** — Requires immediate attention. Covers: CRITICAL, WARNING, FAILURE, SYSTEM_FREEZE, SYSTEM_ERROR.
- **`[STATUS]`** — Informational only. Covers: SYSTEM_BOOT, CHECKIN.

**Recommended Gmail filter:** Subject contains `[STATUS] IceboxHero` → Skip Inbox, Mark as read, Apply label.

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

1. Flash Raspberry Pi OS Lite (32-bit recommended) to your SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
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
git clone git@github.com:crakerjac/IceboxHero.git
cd IceboxHero
sudo ./setup.sh
```

`setup.sh` handles everything else automatically:

- Hardware interfaces (`/boot/firmware/config.txt` — watchdog, SPI, 1-Wire)
- System packages and Python dependencies
- Source code deployment to `/opt/iceboxhero/`
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
http://iceboxhero.local:8080
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
- Chart.js is served locally from `/opt/iceboxhero/static/chart.min.js` — the dashboard loads instantly and works fully during an internet outage.
- Timestamps on the graph are stored in UTC in SQLite and converted to your browser's local timezone automatically for display.
- The dashboard is served on all network interfaces (`0.0.0.0`). It is intended for use on a trusted private network only — there is no authentication.

---

## Operations

### Starting and Stopping Services

Two helper scripts manage the service lifecycle. The critical rule is that the **watchdog must always be stopped before the IceboxHero services** — stopping a service without stopping the watchdog first means the IPC file stops updating, and the watchdog will force a hardware reboot 180 seconds later.

```bash
sudo ./stop_services.sh     # watchdog first, then all five services
sudo ./start_services.sh    # all five services, then watchdog last
```

`start_services.sh` includes two preflight checks before starting anything:

- Confirms at least one DS18B20 sensor is visible on the 1-Wire bus at `/sys/bus/w1/devices/28-*`
- Confirms `config.ini` no longer contains the placeholder sensor ROM IDs

If either check fails it warns you and prompts before continuing. It also waits 5 seconds after starting the IceboxHero services before starting the watchdog, giving `sensor_service` time to write the initial IPC file.

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

## Testing Without Hardware

`mock_sensors.py` replaces `sensor_service.py` for testing before physical sensors arrive. It reads sensor names and thresholds from `config.ini` and writes the same IPC file format all downstream services consume — they can't tell the difference.

### Setup

Stop `icebox-sensor.service` first to avoid race conditions with the IPC file, then run the mock directly:

```bash
sudo systemctl stop icebox-sensor.service

# From the repo directory:
python3 mock_sensors.py --mode sine

# Or if already deployed by setup.sh:
python3 /opt/iceboxhero/mock_sensors.py --mode sine
```

By default the mock uses `poll_interval` from `config.ini` (60 seconds). Use `--interval` to override this for faster testing:

```bash
# Update every second — good for watching ramp and state transitions in real time
python3 mock_sensors.py --mode ramp --interval 1

# Match actual sensor polling without waiting a full minute per step
python3 mock_sensors.py --mode sine --interval 5
```

Note that `--interval` only controls how fast the mock writes the IPC file. The downstream services (`display_service`, `alert_service`, `db_logger`) still run on their own timers — `db_logger` won't commit faster than `db_commit_interval` regardless.

### Modes

| Mode | What it does |
|---|---|
| `sine` (default) | Slow sine wave drifting from normal through warning and critical |
| `normal` | Steady 8°F below warning threshold |
| `warning` | Steady 1°F above warning threshold |
| `critical` | Steady 2°F above critical threshold — triggers buzzer and email after 2 reads |
| `missing` | First sensor returns `None` — triggers FAILURE alert and buzzer |
| `ramp` | Ramps up 1°F per poll cycle, wraps at critical+5 — good for watching state transitions |

### What You Can Test

- **Display** — correct colors, 1 Hz flash at CRITICAL, font sizing, STALE DATA overlay
- **Alerts** — buzzer fires at correct threshold, silence button mutes it, re-arms after 1 hour
- **Email** — CRITICAL and WARNING emails arrive, cooldown prevents flooding, SYSTEM_BOOT on start
- **Web dashboard** — current readings update every 30s, 24-hour graph populates over time
- **Database** — RAM DB writes every 5 minutes, 4-hour SD backup fires, web history graph works
- **Watchdog** — stays quiet while IPC updates; kill the mock script and verify reboot after 180s

Stop the mock with `Ctrl+C`. The IPC file is left in place so downstream services don't immediately go stale — they will naturally time out after `stale_timeout` seconds (default: 600).

---

## Display Identification

Chinese ST7735 modules often ship with no documentation. `display_test.py` cycles through known configurations and pushes test patterns so you can visually confirm which variant you have. On success it writes the working parameters directly to `config.ini`.

> **Wiring note:** The `SDA` and `SCL` labels on these modules are SPI, not I2C. Wire `SDA → GPIO10` (MOSI) and `SCL → GPIO11` (CLK). The `BLK` pin controls the backlight — wire to GPIO18 for software control or to 3.3V for always-on, and set `lcd_bl_pin` in `config.ini` accordingly.

```bash
# List all candidates without touching hardware
python3 display_test.py --list

# Run the interactive identification tool
python3 display_test.py
```

The tool tests solid red, green, and blue fills (BGR vs RGB swap is immediately obvious), then color bars with a resolution label. Enter `a` at the menu to test all candidates in order, or enter a number to jump to a specific one. When you confirm a match the display params are written to `config.ini` and picked up by `display_service.py` on next start.

If nothing matches, check:
- **Blank screen** — VDD (3.3V), GND, and SPI wiring
- **Backlight only** — display init failing silently, try a different candidate
- **Colors shifted** — try BGR=True vs BGR=False variants
- **Image cropped or offset** — try x_offset/y_offset variants

---

## Diagnostics

```bash
# Live log stream from any service
journalctl -u icebox-sensor.service -f
journalctl -u icebox-alert.service -b     # current boot only
journalctl -u icebox-db.service -n 50     # last 50 lines

# Check current sensor readings
cat /run/telemetry_state.json

# Check RAM disk usage
df -h /run

# Check all service status at a glance
systemctl status 'icebox-*'
```

---

## Repository Structure

```
iceboxhero/
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
├── web_server.py                # Flask API and dashboard       (Module 6)
├── mock_sensors.py              # Dev tool — simulates sensors without hardware
├── display_test.py              # Dev tool — identifies display variant, writes config
├── templates/
│   └── index.html               # Web dashboard UI
├── static/
│   └── favicon.png              # Browser tab icon (burning freezer)
├── docs/
│   ├── architecture-overview.png  # Simple data flow diagram — displayed in README
│   ├── architecture-full.png      # Full module diagram — displayed in ARCHITECTURE.md
│   └── ARCHITECTURE.md            # Full design notes, runtime paths, watchdog detail
└── systemd/
    ├── icebox-sensor.service
    ├── icebox-display.service
    ├── icebox-alert.service
    ├── icebox-db.service
    └── icebox-web.service
```

---

## License

GNU General Public License v3.0
