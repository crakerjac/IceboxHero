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
from config_helper import load_config, safe_read_json, get_sensor_configs
import board
import digitalio
from adafruit_rgb_display import st7735

IPC_FILE    = "/run/iceboxhero/telemetry_state.json"
SPLASH_PATH = os.path.join(os.path.dirname(__file__), "static", "splash.jpg")
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
# Splash screen
# ---------------------------------------------------------------------------

_splash_image = None   # Pre-loaded and resized at startup


def load_splash(buf_width, buf_height):
    """Load splash.jpg once at startup, resize to buffer dimensions."""
    global _splash_image
    if not os.path.exists(SPLASH_PATH):
        print(f"WARNING: Splash image not found at {SPLASH_PATH}")
        return
    try:
        img = Image.open(SPLASH_PATH).convert("RGB")
        img = img.resize((buf_width, buf_height), Image.LANCZOS)
        _splash_image = img
        print(f"Splash image loaded: {SPLASH_PATH} → {buf_width}x{buf_height}")
    except Exception as e:
        print(f"WARNING: Failed to load splash image: {e}")


# ---------------------------------------------------------------------------
# Hardware interface
# ---------------------------------------------------------------------------

_display = None   # Module-level display object — initialized once at startup


def init_display(config):
    """Initialize the SPI connection to the ST7735S using adafruit_rgb_display.
    Retries up to 5 times with a delay — the SPI bus may not be fully ready
    immediately at boot, especially when service starts before hardware settles.
    """
    global _display
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

def evaluate_sensor_states(sensor_data, is_stale, sensor_thresholds, critical_counts):
    """Returns per-sensor state dict: {sensor_name: "NORMAL"|"WARNING"|"CRITICAL"}
    Also returns overall worst state for stale/error handling.
    """
    if is_stale:
        # All sensors show CRITICAL on stale data
        return {name: "CRITICAL" for name in sensor_data}, "CRITICAL"

    states = {}
    worst  = "NORMAL"

    for name, temp in sensor_data.items():
        thresholds    = sensor_thresholds.get(name, {})
        temp_warning  = thresholds.get('warning',  10.0)
        temp_critical = thresholds.get('critical', 15.0)

        if temp is None:
            states[name] = "CRITICAL"
            worst = "CRITICAL"
        elif temp >= temp_critical and critical_counts.get(name, 0) >= 2:
            states[name] = "CRITICAL"
            worst = "CRITICAL"
        elif temp >= temp_warning:
            states[name] = "WARNING"
            if worst == "NORMAL":
                worst = "WARNING"
        else:
            states[name] = "NORMAL"

    return states, worst


# ---------------------------------------------------------------------------
# Frame rendering
# ---------------------------------------------------------------------------

def _state_colors(state, flash_toggle):
    """Return (bg_color, text_color) for a given state and flash toggle."""
    if state == "CRITICAL":
        if flash_toggle:
            return (255, 0, 0), (255, 255, 255)   # Red bg, white text
        else:
            return (0, 0, 0), (255, 0, 0)          # Black bg, red text
    elif state == "WARNING":
        return (255, 255, 0), (0, 0, 0)            # Yellow bg, black text (no flash)
    else:
        return (0, 0, 0), (255, 255, 255)           # Black bg, white text


def draw_frame(sensor_data, sensor_order, sensor_states, worst_state, is_stale, width, height):
    """Constructs a Pillow RGB image with per-sensor state coloring.

    Layout:
      1 sensor   — full screen in sensor state color; name centered top, temp centered bottom
      2+ sensors — screen divided into equal horizontal bands, one per sensor;
                   each band rendered independently in its own state color
      STALE      — full screen CRITICAL flash with STALE DATA message
    """
    image = Image.new("RGB", (width, height))
    draw  = ImageDraw.Draw(image)
    PAD   = 4

    # 1 Hz flash toggle — shared across all sensors so they flash in phase
    flash_toggle = int(time.time()) % 2 == 0

    def fit_font(test_str, max_w):
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
        bg, fg = _state_colors("CRITICAL", flash_toggle)
        draw.rectangle((0, 0, width, height), fill=bg)
        msg  = "STALE DATA"
        font = fit_font(msg, width - PAD * 2)
        tw, th = text_wh(msg, font)
        draw.text(((width - tw) // 2, (height - th) // 2), msg, font=font, fill=fg)
        return image

    # Build lines list in sensor_order
    lines = []
    for key in sensor_order:
        temp     = sensor_data.get(key)
        temp_str = "--.-F" if temp is None else f"{temp:.1f}F"
        state    = sensor_states.get(key, "NORMAL")
        lines.append((key, temp_str, state))

    if not lines:
        draw.rectangle((0, 0, width, height), fill=(0, 0, 0))
        return image

    if len(lines) == 1:
        label, temp_str, state = lines[0]
        bg, fg = _state_colors(state, flash_toggle)
        draw.rectangle((0, 0, width, height), fill=bg)

        half = height // 2
        font = fit_font(max(label, temp_str, key=len), width - PAD * 2)

        lw, lh = text_wh(label, font)
        draw.text(((width - lw) // 2, (half - lh) // 2), label, font=font, fill=fg)

        tw, th = text_wh(temp_str, font)
        draw.text(((width - tw) // 2, half + (half - th) // 2), temp_str, font=font, fill=fg)

    else:
        # Divide screen into equal horizontal bands — one per sensor
        LABEL_SIZE = 12
        label_font = get_font(LABEL_SIZE)
        temp_font  = fit_font(max(tmp for _, tmp, _ in lines), width - PAD * 2)

        _, label_h = text_wh("Ag", label_font)
        _, temp_h  = text_wh("Ag", temp_font)
        GROUP_PAD  = 3
        group_h    = label_h + GROUP_PAD + temp_h
        total_h    = group_h * len(lines) + PAD * (len(lines) - 1)
        y_start    = (height - total_h) // 2

        for i, (label, temp_str, state) in enumerate(lines):
            bg, fg      = _state_colors(state, flash_toggle)
            right_align = (i % 2 == 1)

            # Calculate band bounds for background fill
            band_top    = y_start + i * (group_h + PAD)
            band_bottom = band_top + group_h
            draw.rectangle((0, band_top, width, band_bottom), fill=bg)

            y = band_top

            # Label
            lw, _ = text_wh(label, label_font)
            lx = (width - lw - PAD) if right_align else PAD
            draw.text((lx, y), label, font=label_font, fill=fg)
            y += label_h + GROUP_PAD

            # Temp
            tw, _ = text_wh(temp_str, temp_font)
            tx = (width - tw - PAD) if right_align else PAD
            draw.text((tx, y), temp_str, font=temp_font, fill=fg)

    return image


# ---------------------------------------------------------------------------
# Safe JSON reader
# ---------------------------------------------------------------------------


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
    # Hardcoded fallbacks — match default config values
    DC_PIN  = 24
    RST_PIN = 25
    # Use portrait buffer (pre-rotation physical dimensions) as safe fallback
    BUF_W, BUF_H = 128, 160

    print(f"FATAL: config load failed: {error}")
    print(f"Attempting to show CONFIG ERROR on display (DC={DC_PIN}, RST={RST_PIN}).")

    try:
        spi = board.SPI()
        cs  = digitalio.DigitalInOut(board.CE0)
        dc  = digitalio.DigitalInOut(getattr(board, f'D{DC_PIN}'))
        rst = digitalio.DigitalInOut(getattr(board, f'D{RST_PIN}'))
        display = st7735.ST7735R(
            spi, dc=dc, cs=cs, rst=rst,
            width=BUF_W, height=BUF_H, rotation=0, bgr=True,
            baudrate=24000000,
        )

        img  = Image.new("RGB", (BUF_W, BUF_H), (180, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(FONT_PATH, 14)
        except Exception:
            font = ImageFont.load_default()

        # Center text vertically for whatever buffer size we have
        draw.text((4, BUF_H // 5),      "CONFIG",  fill=(255, 255, 255), font=font)
        draw.text((4, BUF_H // 5 + 22), "ERROR",   fill=(255, 255, 255), font=font)
        draw.text((4, BUF_H // 2),      "Check:",  fill=(255, 200, 200), font=font)
        draw.text((4, BUF_H // 2 + 18), "/data/config/config.ini",
                  fill=(255, 200, 200), font=font)

        push_to_display(img)
        print("CONFIG ERROR frame pushed to display.")

    except Exception as disp_err:
        print(f"Display init also failed: {disp_err}. Looping silently.")

    while True:
        time.sleep(60)


def main():
    print("Starting Display Service...")

    try:
        config = load_config()
    except Exception as e:
        _config_error_display(e)
        return  # _config_error_display loops forever, this is defensive

    init_display(config)
    sensor_configs    = get_sensor_configs(config)
    sensor_order      = [s['name'] for s in sensor_configs]
    sensor_thresholds = {s['name']: {'warning': s['warning'], 'critical': s['critical']}
                         for s in sensor_configs}
    refresh_rate  = config.getfloat('display', 'refresh_rate')
    stale_timeout = config.getint('alerts', 'stale_timeout')
    disp_width    = config.getint('display', 'width')
    disp_height   = config.getint('display', 'height')
    rotation      = config.getint('display', 'rotation')
    # The library applies rotation in hardware — image buffer must use
    # physical (pre-rotation) dimensions regardless of logical orientation.
    if rotation in (90, 270):
        buf_width, buf_height = disp_height, disp_width
    else:
        buf_width, buf_height = disp_width, disp_height

    splash_duration      = config.getint('display', 'splash_duration', fallback=60)
    last_ipc_timestamp   = 0
    critical_read_counts = {}
    parse_error_count    = 0   # Consecutive JSON parse failures before alarming
    # Grace mode: hold neutral display state until the first real sensor read
    # arrives (IPC timestamp > 0). This prevents false CRITICAL flashes between
    # splash end and first sensor poll regardless of timing.
    first_real_read      = False

    # Clear any stale framebuffer content immediately after init —
    # the hardware holds the last image until we push something new.
    push_to_display(Image.new("RGB", (buf_width, buf_height), (0, 0, 0)))

    # Load splash image and show it immediately
    load_splash(buf_width, buf_height)
    if _splash_image is not None and splash_duration > 0:
        push_to_display(_splash_image)
        print(f"Showing splash screen (max {splash_duration}s, exits early on first sensor read)...")
        splash_start = time.monotonic()
        while True:
            elapsed = time.monotonic() - splash_start
            # Exit early if real sensor data arrives
            if os.path.exists(IPC_FILE):
                payload = safe_read_json(IPC_FILE)
                if payload and isinstance(payload, dict):
                    sd = payload.get("sensors", {})
                    ts = payload.get("timestamp", 0)
                    if ts > 0 and any(v is not None for v in sd.values()):
                        first_real_read = True
                        print(f"First real sensor read detected after {elapsed:.1f}s — ending splash early.")
                        break
            # Exit after maximum splash_duration regardless of sensor state
            if elapsed >= splash_duration:
                print(f"Splash duration reached ({splash_duration}s) — entering normal operation.")
                break
            time.sleep(0.5)
        print("Splash complete — starting normal display loop.")

    while True:
        is_stale     = False
        sensor_data  = {}
        state        = "NORMAL"
        sensor_states = {}
        if not os.path.exists(IPC_FILE):
            state = "NORMAL"   # Show empty/booting state
        else:
            mtime = os.path.getmtime(IPC_FILE)
            if first_real_read and (time.time() - mtime) > stale_timeout:
                is_stale = True

            try:
                payload = safe_read_json(IPC_FILE)

                if payload is None:
                    # Stay neutral until first real read confirmed
                    if first_real_read:
                        parse_error_count += 1
                        if parse_error_count >= 3:
                            state = "CRITICAL"
                else:
                    parse_error_count = 0
                    sensor_data   = payload.get("sensors", {})
                    ipc_timestamp = payload.get("timestamp", 0)

                    # Detect first real sensor read: timestamp > 0 and at least
                    # one sensor has a non-None value
                    if not first_real_read and ipc_timestamp > 0 and                        any(v is not None for v in sensor_data.values()):
                        first_real_read = True
                        print("First real sensor read received — entering normal display mode.")

                    # Only update critical counters on a new sensor poll
                    if ipc_timestamp != last_ipc_timestamp:
                        last_ipc_timestamp = ipc_timestamp
                        for name, temp in sensor_data.items():
                            if temp is not None:
                                t = sensor_thresholds.get(name, {})
                                crit = t.get('critical', 15.0)
                                if temp >= crit:
                                    critical_read_counts[name] = critical_read_counts.get(name, 0) + 1
                                else:
                                    critical_read_counts[name] = 0
                            # None reading: leave counter unchanged — a dead sensor
                            # during an active critical condition should not reset the alarm

                    if first_real_read:
                        sensor_states, state = evaluate_sensor_states(
                            sensor_data, is_stale, sensor_thresholds, critical_read_counts
                        )

            except (json.JSONDecodeError, KeyError):
                if first_real_read:
                    parse_error_count += 1
                    if parse_error_count >= 3:
                        state = "CRITICAL"

        if not sensor_states:
            sensor_states = {name: state for name in sensor_order}
        frame = draw_frame(sensor_data, sensor_order, sensor_states, state, is_stale, buf_width, buf_height)
        push_to_display(frame)

        time.sleep(refresh_rate)


if __name__ == '__main__':
    main()
