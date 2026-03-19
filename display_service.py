"""
Module 3 — Display Service (display_service.py)

Reads /run/iceboxhero/telemetry_state.json every 500 ms and renders the current
temperature state to the ST7735S LCD via Pillow frame-buffer rendering.

Display states:
  NORMAL   — White text on Black background
  WARNING  — Black text on Yellow background
  CRITICAL — Flashes White-on-Red ↔ Red-on-Black at 1 Hz
  STALE    — Overwrites with "STALE DATA" in CRITICAL colors

Font sizing: dynamically fits the largest DejaVu Bold font (size 40 down to 9)
that fills the 160 px display width with 2 px side padding. Font objects are
cached at startup to avoid repeated FreeType allocations in the 500 ms loop.

Driver: adafruit_rgb_display.st7735 + Pillow. Run display_test.py to identify
your display variant and auto-populate display settings in config.ini.
"""

import os
import time
import json
from PIL import Image, ImageDraw, ImageFont
from config_helper import load_config
import board
import digitalio
from adafruit_rgb_display import st7735

IPC_FILE  = "/run/iceboxhero/telemetry_state.json"
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
# Hardware interface
# ---------------------------------------------------------------------------

_display = None   # Module-level display object — initialized once at startup


def init_display():
    """Initialize the SPI connection to the ST7735S using adafruit_rgb_display.
    Retries up to 5 times with a delay — the SPI bus may not be fully ready
    immediately at boot, especially when service starts before hardware settles.
    """
    global _display
    config  = load_config()
    dc_pin  = config.getint('hardware', 'lcd_dc_pin',  fallback=24)
    width   = config.getint('display',  'width',       fallback=128)
    height  = config.getint('display',  'height',      fallback=160)
    rotation= config.getint('display',  'rotation',    fallback=0)
    bgr     = config.getboolean('display', 'bgr',      fallback=True)
    x_off   = config.getint('display',  'x_offset',    fallback=0)
    y_off   = config.getint('display',  'y_offset',    fallback=0)

    rst_pin = config.getint('hardware', 'lcd_rst_pin', fallback=25)

    for attempt in range(1, 6):
        try:
            spi = board.SPI()
            cs  = digitalio.DigitalInOut(board.CE0)
            dc  = digitalio.DigitalInOut(getattr(board, f'D{dc_pin}'))
            rst = digitalio.DigitalInOut(getattr(board, f'D{rst_pin}'))

            _display = st7735.ST7735R(
                spi,
                dc=dc,
                cs=cs,
                rst=rst,
                width=width,
                height=height,
                rotation=rotation,
                bgr=bgr,
                x_offset=x_off,
                y_offset=y_off,
                baudrate=24000000,
            )
            print(f"Display initialized: {width}x{height} rotation={rotation} bgr={bgr}")
            return
        except Exception as e:
            print(f"Display init attempt {attempt}/5 failed: {e}")
            time.sleep(2)

    print("WARNING: Display failed to initialize after 5 attempts. Running without display.")


def push_to_display(image):
    """Push a Pillow RGB image to the hardware frame buffer."""
    if _display is not None:
        _display.image(image)


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

def draw_frame(sensor_data, sensor_order, state, is_stale, width, height):
    """Constructs a Pillow RGB image with the appropriate colors and flash logic.

    Layout:
      1 sensor   — name centered top half, temp centered bottom half
      2+ sensors — one line per sensor: name left-aligned, temp right-aligned
                   font sized to fit the longest line
      STALE/ERROR — single centered message
    """
    image = Image.new("RGB", (width, height))
    draw  = ImageDraw.Draw(image)
    PAD   = 4

    # 1 Hz flash: int(time.time()) % 2 toggles once per second
    flash_toggle = int(time.time()) % 2 == 0

    if state == "CRITICAL":
        if flash_toggle:
            bg_color, text_color = (255, 0, 0), (255, 255, 255)
        else:
            bg_color, text_color = (0, 0, 0), (255, 0, 0)
    elif state == "WARNING":
        bg_color, text_color = (255, 255, 0), (0, 0, 0)
    else:
        bg_color, text_color = (0, 0, 0), (255, 255, 255)

    draw.rectangle((0, 0, width, height), fill=bg_color)

    def fit_font(test_str, max_w):
        """Return largest font that fits test_str within max_w pixels."""
        for fs in range(40, 8, -1):
            f  = get_font(fs)
            bb = draw.textbbox((0, 0), test_str, font=f)
            if (bb[2] - bb[0]) <= max_w:
                return f
        return get_font(9)

    def text_wh(s, f):
        bb = draw.textbbox((0, 0), s, font=f)
        return bb[2] - bb[0], bb[3] - bb[1]

    if is_stale:
        msg  = "STALE DATA"
        font = fit_font(msg, width - PAD * 2)
        tw, th = text_wh(msg, font)
        draw.text(((width - tw) // 2, (height - th) // 2), msg, font=font, fill=text_color)
        return image

    # Build (label, temp_str) pairs in sensor_order
    lines = []
    for key in sensor_order:
        temp     = sensor_data.get(key)
        label    = key              # Full sensor name from config
        temp_str = "--.-F" if temp is None else f"{temp:.1f}F"
        lines.append((label, temp_str))

    if not lines:
        lines = [("", "NO DATA")]

    if len(lines) == 1:
        label, temp_str = lines[0]
        half = height // 2
        font = fit_font(max(label, temp_str, key=len), width - PAD * 2)

        lw, lh = text_wh(label, font)
        draw.text(((width - lw) // 2, (half - lh) // 2), label, font=font, fill=text_color)

        tw, th = text_wh(temp_str, font)
        draw.text(((width - tw) // 2, half + (half - th) // 2), temp_str, font=font, fill=text_color)

    else:
        # Two-font layout: small label above large temp, alternating left/right alignment.
        # Label uses a fixed small font; temp font is sized to fit the longest temp string.
        LABEL_SIZE = 12
        label_font = get_font(LABEL_SIZE)
        temp_font  = fit_font(max(tmp for _, tmp in lines), width - PAD * 2)

        _, label_h = text_wh("Ag", label_font)
        _, temp_h  = text_wh("Ag", temp_font)
        GROUP_PAD  = 3   # gap between label and its temp
        PAIR_PAD   = 8   # gap between the two sensor groups

        group_h = label_h + GROUP_PAD + temp_h
        total_h = len(lines) * group_h + (len(lines) - 1) * PAIR_PAD
        y       = (height - total_h) // 2

        for i, (label, temp_str) in enumerate(lines):
            right_align = (i % 2 == 1)  # even=left, odd=right

            # Draw label
            lw, _ = text_wh(label, label_font)
            lx = (width - lw - PAD) if right_align else PAD
            draw.text((lx, y), label, font=label_font, fill=text_color)
            y += label_h + GROUP_PAD

            # Draw temp
            tw, _ = text_wh(temp_str, temp_font)
            tx = (width - tw - PAD) if right_align else PAD
            draw.text((tx, y), temp_str, font=temp_font, fill=text_color)
            y += temp_h + PAIR_PAD

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

def _config_error_display(error):
    """
    Last-resort handler for config load failure.
    Pushes a CONFIG ERROR frame to the display using hardcoded pin fallbacks,
    then loops forever so the message stays visible and systemd doesn't
    restart-loop. Healthchecks.io heartbeat stops → email arrives after timeout.
    """
    DC_PIN  = 24   # Hardcoded fallbacks — match default config
    RST_PIN = 25

    print(f"FATAL: config load failed: {error}")
    print(f"Attempting to show CONFIG ERROR on display (DC={DC_PIN}, RST={RST_PIN}).")

    try:
        spi = board.SPI()
        cs  = digitalio.DigitalInOut(board.CE0)
        dc  = digitalio.DigitalInOut(getattr(board, f'D{DC_PIN}'))
        display = st7735.ST7735R(
            spi, dc=dc, cs=cs, rst=None,
            width=128, height=160, rotation=0, bgr=True,
            baudrate=24000000,
        )

        img  = Image.new("RGB", (128, 160), (180, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(FONT_PATH, 14)
        except Exception:
            font = ImageFont.load_default()

        draw.text((4,  20), "CONFIG",  fill=(255, 255, 255), font=font)
        draw.text((4,  42), "ERROR",   fill=(255, 255, 255), font=font)
        draw.text((4,  74), "Check:",  fill=(255, 200, 200), font=font)
        draw.text((4,  92), "/data/",  fill=(255, 200, 200), font=font)
        draw.text((4, 108), "config/", fill=(255, 200, 200), font=font)
        draw.text((4, 124), "config",  fill=(255, 200, 200), font=font)
        draw.text((4, 140), ".ini",    fill=(255, 200, 200), font=font)

        push_to_display(img)
        print("CONFIG ERROR frame pushed to display.")

    except Exception as disp_err:
        print(f"Display init also failed: {disp_err}. Looping silently.")

    while True:
        time.sleep(60)


def main():
    print("Starting Display Service...")
    init_display()

    try:
        config = load_config()
    except Exception as e:
        _config_error_display(e)
    sensor_order  = sorted(dict(config.items('sensors')).values())
    refresh_rate  = config.getfloat('display', 'refresh_rate')
    stale_timeout = config.getint('display', 'stale_timeout')
    temp_warning  = config.getfloat('sampling', 'temp_warning')
    temp_critical = config.getfloat('sampling', 'temp_critical')
    disp_width    = config.getint('display', 'width')
    disp_height   = config.getint('display', 'height')
    rotation      = config.getint('display', 'rotation')
    # The library applies rotation in hardware — image buffer must use
    # physical (pre-rotation) dimensions regardless of logical orientation.
    if rotation in (90, 270):
        buf_width, buf_height = disp_height, disp_width
    else:
        buf_width, buf_height = disp_width, disp_height

    last_ipc_timestamp  = 0
    critical_read_counts = {}

    while True:
        is_stale    = False
        sensor_data = {}
        state       = "NORMAL"

        if not os.path.exists(IPC_FILE):
            state = "NORMAL"   # Show empty/booting state
        else:
            mtime = os.path.getmtime(IPC_FILE)
            if (time.time() - mtime) > stale_timeout:
                is_stale = True

            try:
                payload = safe_read_json(IPC_FILE)

                if payload is None:
                    state = "CRITICAL"
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

                    state = evaluate_worst_state(
                        sensor_data, is_stale, temp_warning, temp_critical, critical_read_counts
                    )

            except (json.JSONDecodeError, KeyError):
                state = "CRITICAL"

        frame = draw_frame(sensor_data, sensor_order, state, is_stale, buf_width, buf_height)
        push_to_display(frame)

        time.sleep(refresh_rate)


if __name__ == '__main__':
    main()
