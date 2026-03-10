"""
Module 3 — Display Service (display_service.py)

Reads /run/freezerpi/telemetry_state.json every 500 ms and renders the current
temperature state to the ST7735S LCD via Pillow frame-buffer rendering.

Display states:
  NORMAL   — White text on Black background
  WARNING  — Black text on Yellow background
  CRITICAL — Flashes White-on-Red ↔ Red-on-Black at 1 Hz
  STALE    — Overwrites with "STALE DATA" in CRITICAL colors

Font sizing: dynamically fits the largest DejaVu Bold font (size 40 down to 9)
that fills the 160 px display width with 2 px side padding. Font objects are
cached at startup to avoid repeated FreeType allocations in the 500 ms loop.

NOTE: Populate init_display() and push_to_display() with your specific
ST7735 SPI driver calls. See README for driver variant identification.
"""

import os
import time
import json
from PIL import Image, ImageDraw, ImageFont
from config_helper import load_config
# Import your specific ST7735 SPI driver here, e.g.:
# import board, busio, digitalio
# import adafruit_rgb_display.st7735 as st7735

IPC_FILE  = "/run/freezerpi/telemetry_state.json"
WIDTH     = 160
HEIGHT    = 128
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


# ---------------------------------------------------------------------------
# Font cache — pre-load all sizes once at startup to avoid repeated file I/O
# and FreeType allocations inside the 500 ms display loop.
# ---------------------------------------------------------------------------
_font_cache = {}


def get_font(font_size):
    if font_size not in _font_cache:
        _font_cache[font_size] = ImageFont.truetype(FONT_PATH, font_size)
    return _font_cache[font_size]


# ---------------------------------------------------------------------------
# Hardware interface stubs — replace with your ST7735 driver calls
# ---------------------------------------------------------------------------

def init_display():
    """Initialize the SPI connection to the ST7735S. Populate with driver calls."""
    pass


def push_to_display(image):
    """Push a Pillow RGB image to the hardware frame buffer."""
    pass


# ---------------------------------------------------------------------------
# State evaluation
# ---------------------------------------------------------------------------

def evaluate_worst_state(sensor_data, is_stale, temp_warning, temp_critical, critical_counts):
    """Returns the worst-case display state across all sensors."""
    if is_stale:
        return "CRITICAL"

    worst_state = "NORMAL"

    for name, temp in sensor_data.items():
        if temp is None:
            return "CRITICAL"
        elif temp >= temp_critical and critical_counts.get(name, 0) >= 2:
            return "CRITICAL"
        elif temp >= temp_warning:
            if worst_state == "NORMAL":
                worst_state = "WARNING"

    return worst_state


def format_temperature_string(sensor_data, sensor_order):
    """Formats temperatures in alphabetical sensor order for consistent layout."""
    parts = []
    for key in sensor_order:
        temp = sensor_data.get(key)
        parts.append("--.-F" if temp is None else f"{temp:.1f}F")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Frame rendering
# ---------------------------------------------------------------------------

def draw_frame(text, state, is_stale):
    """Constructs a Pillow RGB image with the appropriate colors and flash logic."""
    image = Image.new("RGB", (WIDTH, HEIGHT))
    draw  = ImageDraw.Draw(image)

    # 1 Hz flash: int(time.time()) % 2 toggles once per second;
    # the 500 ms loop guarantees we catch each transition.
    flash_toggle = int(time.time()) % 2 == 0

    if state == "CRITICAL":
        if flash_toggle:
            bg_color, text_color = (255, 0, 0), (255, 255, 255)  # Red bg, White text
        else:
            bg_color, text_color = (0, 0, 0), (255, 0, 0)        # Black bg, Red text
    elif state == "WARNING":
        bg_color, text_color = (255, 255, 0), (0, 0, 0)           # Yellow bg, Black text
    else:
        bg_color, text_color = (0, 0, 0), (255, 255, 255)         # Black bg, White text

    draw.rectangle((0, 0, WIDTH, HEIGHT), fill=bg_color)

    display_text = "STALE DATA" if is_stale else text

    # Fit the largest font size that stays within display width with 2 px padding
    for font_size in range(40, 8, -1):
        font      = get_font(font_size)
        test_bbox = draw.textbbox((0, 0), display_text, font=font)
        if (test_bbox[2] - test_bbox[0]) <= WIDTH - 4:
            break

    text_bbox = draw.textbbox((0, 0), display_text, font=font)
    text_w    = text_bbox[2] - text_bbox[0]
    text_h    = text_bbox[3] - text_bbox[1]
    x = (WIDTH  - text_w) // 2
    y = (HEIGHT - text_h) // 2

    draw.text((x, y), display_text, font=font, fill=text_color)
    return image


# ---------------------------------------------------------------------------
# Safe JSON reader
# ---------------------------------------------------------------------------

def safe_read_json(path, retries=3):
    for _ in range(retries):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print("Starting Display Service...")
    init_display()

    config        = load_config()
    sensor_order  = sorted(dict(config.items('sensors')).values())
    refresh_rate  = config.getfloat('display', 'refresh_rate')
    stale_timeout = config.getint('display', 'stale_timeout')
    temp_warning  = config.getfloat('sampling', 'temp_warning')
    temp_critical = config.getfloat('sampling', 'temp_critical')

    last_ipc_timestamp  = 0
    critical_read_counts = {}

    while True:
        is_stale     = False
        sensor_data  = {}
        display_text = "BOOTING..."
        state        = "NORMAL"

        if not os.path.exists(IPC_FILE):
            display_text = "BOOTING..."
            state        = "NORMAL"
        else:
            mtime = os.path.getmtime(IPC_FILE)
            if (time.time() - mtime) > stale_timeout:
                is_stale = True

            try:
                payload = safe_read_json(IPC_FILE)

                if payload is None:
                    display_text = "READ ERROR"
                    state        = "CRITICAL"
                else:
                    sensor_data   = payload.get("sensors", {})
                    ipc_timestamp = payload.get("timestamp", 0)

                    # Only update critical counters on a new sensor poll
                    if ipc_timestamp != last_ipc_timestamp:
                        last_ipc_timestamp = ipc_timestamp
                        for name, temp in sensor_data.items():
                            if temp is not None and temp >= temp_critical:
                                critical_read_counts[name] = critical_read_counts.get(name, 0) + 1
                            else:
                                critical_read_counts[name] = 0

                    display_text = format_temperature_string(sensor_data, sensor_order)
                    state        = evaluate_worst_state(
                        sensor_data, is_stale, temp_warning, temp_critical, critical_read_counts
                    )

            except (json.JSONDecodeError, KeyError):
                display_text = "FILE ERROR"
                state        = "CRITICAL"

        frame = draw_frame(display_text, state, is_stale)
        push_to_display(frame)

        time.sleep(refresh_rate)


if __name__ == '__main__':
    main()
