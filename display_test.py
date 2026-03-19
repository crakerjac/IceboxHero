#!/usr/bin/env python3
"""
display_test.py — IceboxHero Display Identification Tool

Cycles through known ST7735 configurations and pushes test patterns so you
can visually identify your display. On success, writes the working parameters
to /data/config/config.ini so display_service.py picks them up automatically.

Usage:
    python3 display_test.py           — interactive menu
    python3 display_test.py --list    — list all candidates without testing

Pin wiring (configured in config.ini [hardware]):
    SCL  → GPIO11 (SPI CLK,  physical pin 23)
    SDA  → GPIO10 (SPI MOSI, physical pin 19)
    CS   → GPIO8  (SPI CE0,  physical pin 24)
    DC   → GPIO24             (physical pin 18)
    RST  → GPIO25             (physical pin 22)
    BLK  → GPIO18 or 3.3V    (physical pin 12 or pin 17)
    VDD  → 3.3V               (physical pin 1 or 17)
    GND  → GND                (physical pin 6, 9, 14, 20, 25, 30, 34, or 39)

Note: The SDA label on Chinese ST7735 modules is SPI MOSI, not I2C.
"""

import sys
import time
import argparse
import configparser

CONFIG_PATH = "/data/config/config.ini"

# =============================================================================
# Candidate display configurations — ranked by likelihood for 128x160 modules
# =============================================================================
CANDIDATES = [
    {
        "name":     "BLACKTAB — BGR, no offset (most common bare module)",
        "bgr":      True,
        "x_offset": 0,
        "y_offset": 0,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "BLACKTAB — RGB, no offset",
        "bgr":      False,
        "x_offset": 0,
        "y_offset": 0,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "REDTAB — BGR, no offset (common Chinese clone)",
        "bgr":      True,
        "x_offset": 0,
        "y_offset": 0,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "GREENTAB — BGR, x_offset=1 y_offset=2 (Adafruit original)",
        "bgr":      True,
        "x_offset": 1,
        "y_offset": 2,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "GREENTAB — RGB, x_offset=1 y_offset=2",
        "bgr":      False,
        "x_offset": 1,
        "y_offset": 2,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "BLACKTAB — BGR, no offset, rotation=90",
        "bgr":      True,
        "x_offset": 0,
        "y_offset": 0,
        "width":    128,
        "height":   160,
        "rotation": 90,
    },
    {
        "name":     "BLACKTAB — BGR, no offset, rotation=180",
        "bgr":      True,
        "x_offset": 0,
        "y_offset": 0,
        "width":    128,
        "height":   160,
        "rotation": 180,
    },
    {
        "name":     "GREENTAB128 — BGR, y_offset=32 (128x128 variant)",
        "bgr":      True,
        "x_offset": 0,
        "y_offset": 32,
        "width":    128,
        "height":   128,
        "rotation": 0,
    },
]

# =============================================================================
# Config helpers
# =============================================================================

def load_config():
    config = configparser.ConfigParser()
    if not config.read(CONFIG_PATH):
        print(f"[ERROR] Could not read config at {CONFIG_PATH}")
        print("        Run setup.sh and edit config.ini before using this tool.")
        sys.exit(1)
    return config


def get_pin(config, key, fallback=None):
    val = config.get('hardware', key, fallback=str(fallback) if fallback else 'none')
    if val.lower() == 'none':
        return None
    try:
        return int(val)
    except ValueError:
        print(f"[WARN] Could not parse hardware.{key} = {val!r}, ignoring")
        return None


def write_display_config(candidate):
    """Write working display parameters back to config.ini."""
    config = load_config()
    if not config.has_section('display'):
        config.add_section('display')
    config.set('display', 'width',    str(candidate['width']))
    config.set('display', 'height',   str(candidate['height']))
    config.set('display', 'rotation', str(candidate['rotation']))
    config.set('display', 'bgr',      str(candidate['bgr']))
    config.set('display', 'x_offset', str(candidate['x_offset']))
    config.set('display', 'y_offset', str(candidate['y_offset']))
    with open(CONFIG_PATH, 'w') as f:
        config.write(f)
    print(f"\n[OK] Display config written to {CONFIG_PATH}")
    print("     display_service.py will use these settings on next start.")


# =============================================================================
# Display driver
# =============================================================================

def init_display(config, candidate):
    """
    Initialise the ST7735 display with the given candidate config.
    Uses adafruit_rgb_display.st7735 + Pillow — the same stack as display_service.py.
    Returns (display, backlight_pin_obj | None) or raises on failure.
    """
    try:
        import board
        import digitalio
        from adafruit_rgb_display import st7735
    except ImportError as e:
        print(f"\n[ERROR] Missing library: {e}")
        print("        Run: pip3 install adafruit-circuitpython-rgb-display --break-system-packages")
        sys.exit(1)

    dc_pin  = get_pin(config, 'lcd_dc_pin',  24)
    rst_pin = get_pin(config, 'lcd_rst_pin', 25)
    bl_pin  = get_pin(config, 'lcd_bl_pin',  None)

    # Use board.SPI() — the adafruit_rgb_display library on Linux needs the
    # hardware SPI bus object, not a manually constructed busio.SPI instance.
    spi = board.SPI()
    cs  = digitalio.DigitalInOut(board.CE0)
    dc  = digitalio.DigitalInOut(getattr(board, f'D{dc_pin}'))
    rst = digitalio.DigitalInOut(getattr(board, f'D{rst_pin}'))

    # Enable backlight if pin configured
    backlight = None
    if bl_pin is not None:
        backlight = digitalio.DigitalInOut(getattr(board, f'D{bl_pin}'))
        backlight.direction = digitalio.Direction.OUTPUT
        backlight.value = True
        print(f"  Backlight enabled on GPIO{bl_pin}")
    else:
        # No bl_pin in config — try GPIO18 anyway so the screen is visible during testing.
        # If BLK is wired directly to 3.3V this is harmless.
        try:
            backlight = digitalio.DigitalInOut(board.D18)
            backlight.direction = digitalio.Direction.OUTPUT
            backlight.value = True
            print("  Backlight forced ON (GPIO18) for testing")
        except Exception:
            print("  Backlight pin = none (assuming wired to 3.3V)")

    display = st7735.ST7735R(
        spi,
        dc=dc,
        cs=cs,
        rst=rst,
        width=candidate['width'],
        height=candidate['height'],
        rotation=candidate['rotation'],
        bgr=candidate['bgr'],
        x_offset=candidate['x_offset'],
        y_offset=candidate['y_offset'],
        baudrate=24000000,
    )

    return display, backlight


def push_test_pattern(display, candidate):
    """
    Push a sequence of test patterns using Pillow — same stack as display_service.py.
      1. Solid red fill   — catches BGR swap (shows blue if wrong)
      2. Solid green fill
      3. Solid blue fill  — catches BGR swap (shows red if wrong)
      4. Color bars + resolution label
    Each pattern holds for 2 seconds.
    """
    from PIL import Image, ImageDraw, ImageFont

    w = candidate['width']
    h = candidate['height']

    def push(img):
        display.image(img)

    def solid(color):
        img = Image.new("RGB", (w, h), color)
        push(img)
        time.sleep(2)

    print("    → Solid RED   (should look red,   not blue)")
    solid((255, 0, 0))
    print("    → Solid GREEN (should look green)")
    solid((0, 255, 0))
    print("    → Solid BLUE  (should look blue,  not red)")
    solid((0, 0, 255))

    # Color bars + resolution label
    print("    → Color bars + label")
    img  = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    bar  = w // 4
    draw.rectangle([0,        0, bar - 1,     h], fill=(255, 0,   0))
    draw.rectangle([bar,      0, bar * 2 - 1, h], fill=(0,   255, 0))
    draw.rectangle([bar * 2,  0, bar * 3 - 1, h], fill=(0,   0,   255))
    draw.rectangle([bar * 3,  0, w - 1,       h], fill=(255, 255, 255))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
    draw.text((4, h - 20), f"{w}x{h}", fill=(0, 0, 0), font=font)
    push(img)
    time.sleep(3)


# =============================================================================
# Main
# =============================================================================

def list_candidates():
    print("\nKnown ST7735 configurations:\n")
    for i, c in enumerate(CANDIDATES):
        print(f"  {i + 1:2d}. {c['name']}")
        print(f"       {c['width']}x{c['height']}  rotation={c['rotation']}  "
              f"bgr={c['bgr']}  x_offset={c['x_offset']}  y_offset={c['y_offset']}")
    print()


def run_interactive():
    config = load_config()

    print("\n" + "=" * 60)
    print("  IceboxHero — Display Identification Tool")
    print("=" * 60)
    print(f"\n  Config:    {CONFIG_PATH}")
    print(f"  DC pin:    GPIO{get_pin(config, 'lcd_dc_pin',  24)}")
    print(f"  RST pin:   GPIO{get_pin(config, 'lcd_rst_pin', 25)}")
    bl = get_pin(config, 'lcd_bl_pin', None)
    print(f"  BLK pin:   {'GPIO' + str(bl) if bl else '3.3V (always-on)'}")
    print(f"  SPI MOSI:  GPIO10 (SDA on your module)")
    print(f"  SPI CLK:   GPIO11 (SCL on your module)")
    print(f"  SPI CS:    GPIO8  (CE0)")
    print()

    list_candidates()

    print("Enter a candidate number to test, 'a' to test all in order,")
    print("or 'q' to quit without saving.\n")

    while True:
        try:
            choice = input("Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

        if choice == 'q':
            print("Exiting without saving.")
            sys.exit(0)

        candidates_to_test = []

        if choice == 'a':
            candidates_to_test = list(range(len(CANDIDATES)))
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(CANDIDATES):
                    candidates_to_test = [idx]
                else:
                    print(f"  Please enter a number between 1 and {len(CANDIDATES)}")
                    continue
            except ValueError:
                print("  Invalid input.")
                continue

        for idx in candidates_to_test:
            candidate = CANDIDATES[idx]
            print(f"\nTesting: {candidate['name']}")
            print("  Initialising display...")

            try:
                display, backlight = init_display(config, candidate)
            except Exception as e:
                print(f"  [ERROR] Init failed: {e}")
                continue

            print("  Pushing test patterns (8 seconds total)...")
            try:
                push_test_pattern(display, candidate)
            except Exception as e:
                import traceback
                print(f"  [ERROR] Pattern failed: {e}")
                traceback.print_exc()
                input("  (press Enter to continue)")
                if backlight:
                    backlight.value = False
                continue

            if backlight:
                backlight.value = False

            try:
                result = input("\n  Did the display show correct colors and fill the screen? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                sys.exit(0)

            if result == 'y':
                print(f"\n[OK] Matched: {candidate['name']}")
                write_display_config(candidate)
                print("\nNext steps:")
                print("  1. Restart display_service: sudo systemctl restart icebox-display.service")
                print("  2. Or run the full start: sudo ./start_services.sh")
                sys.exit(0)
            else:
                print("  Moving on...")

        print("\nNo match confirmed. Try individual candidates or check your wiring.")
        print("Common issues:")
        print("  - Colors all wrong → try BGR=True vs BGR=False variants")
        print("  - Image shifted/cropped → try x_offset/y_offset variants")
        print("  - Blank screen → check VDD (3.3V), GND, and SPI wiring")
        print("  - Backlight only → display init may be failing silently")


def main():
    parser = argparse.ArgumentParser(description="IceboxHero display identification tool")
    parser.add_argument('--list', action='store_true', help='List all candidates without testing')
    args = parser.parse_args()

    if args.list:
        list_candidates()
    else:
        run_interactive()


if __name__ == '__main__':
    main()
