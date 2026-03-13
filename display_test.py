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
        "rowstart": 0,
        "colstart": 0,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "BLACKTAB — RGB, no offset",
        "bgr":      False,
        "rowstart": 0,
        "colstart": 0,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "REDTAB — BGR, no offset (common Chinese clone)",
        "bgr":      True,
        "rowstart": 0,
        "colstart": 0,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "GREENTAB — BGR, rowstart=2 colstart=1 (Adafruit original)",
        "bgr":      True,
        "rowstart": 2,
        "colstart": 1,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "GREENTAB — RGB, rowstart=2 colstart=1",
        "bgr":      False,
        "rowstart": 2,
        "colstart": 1,
        "width":    128,
        "height":   160,
        "rotation": 0,
    },
    {
        "name":     "BLACKTAB — BGR, rowstart=0 colstart=0, rotation=90",
        "bgr":      True,
        "rowstart": 0,
        "colstart": 0,
        "width":    128,
        "height":   160,
        "rotation": 90,
    },
    {
        "name":     "BLACKTAB — BGR, rowstart=0 colstart=0, rotation=180",
        "bgr":      True,
        "rowstart": 0,
        "colstart": 0,
        "width":    128,
        "height":   160,
        "rotation": 180,
    },
    {
        "name":     "GREENTAB128 — BGR, rowstart=32 colstart=0 (128x128 variant)",
        "bgr":      True,
        "rowstart": 32,
        "colstart": 0,
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
    config.set('display', 'rowstart', str(candidate['rowstart']))
    config.set('display', 'colstart', str(candidate['colstart']))
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
    Returns (display, backlight_pin_obj | None) or raises on failure.
    """
    try:
        import board
        import busio
        import digitalio
        import adafruit_st7735r
    except ImportError as e:
        print(f"\n[ERROR] Missing library: {e}")
        print("        Run: pip3 install adafruit-circuitpython-st7735r --break-system-packages")
        sys.exit(1)

    dc_pin  = get_pin(config, 'lcd_dc_pin',  24)
    rst_pin = get_pin(config, 'lcd_rst_pin', 25)
    bl_pin  = get_pin(config, 'lcd_bl_pin',  None)

    spi = busio.SPI(clock=board.SCLK, MOSI=board.MOSI)

    dc  = digitalio.DigitalInOut(getattr(board, f'D{dc_pin}'))
    rst = digitalio.DigitalInOut(getattr(board, f'D{rst_pin}'))
    cs  = digitalio.DigitalInOut(board.CE0)

    # Enable backlight if pin configured
    backlight = None
    if bl_pin is not None:
        backlight = digitalio.DigitalInOut(getattr(board, f'D{bl_pin}'))
        backlight.direction = digitalio.Direction.OUTPUT
        backlight.value = True
        print(f"  Backlight enabled on GPIO{bl_pin}")
    else:
        print("  Backlight pin = none (assuming wired to 3.3V)")

    display = adafruit_st7735r.ST7735R(
        spi,
        dc=dc,
        cs=cs,
        rst=rst,
        width=candidate['width'],
        height=candidate['height'],
        rotation=candidate['rotation'],
        bgr=candidate['bgr'],
        rowstart=candidate['rowstart'],
        colstart=candidate['colstart'],
    )

    return display, backlight


def push_test_pattern(display, candidate):
    """
    Push a sequence of test patterns:
      1. Solid red fill   — catches BGR swap (shows blue if wrong)
      2. Solid green fill
      3. Solid blue fill  — catches BGR swap (shows red if wrong)
      4. Color bars + label text
    Each pattern holds for 2 seconds.
    """
    try:
        import displayio
        import terminalio
        from adafruit_display_text import label
    except ImportError as e:
        print(f"\n[ERROR] Missing library: {e}")
        print("        Run: pip3 install adafruit-display-text --break-system-packages")
        sys.exit(1)

    w = candidate['width']
    h = candidate['height']

    def solid_fill(color_565):
        bmp = displayio.Bitmap(w, h, 1)
        pal = displayio.Palette(1)
        pal[0] = color_565
        tg  = displayio.TileGrid(bmp, pixel_shader=pal)
        grp = displayio.Group()
        grp.append(tg)
        display.root_group = grp
        time.sleep(2)

    print("    → Solid RED   (should look red,   not blue)")
    solid_fill(0xFF0000)
    print("    → Solid GREEN (should look green)")
    solid_fill(0x00FF00)
    print("    → Solid BLUE  (should look blue,  not red)")
    solid_fill(0x0000FF)

    # Color bars + candidate name
    print("    → Color bars + label")
    bar_w  = w // 4
    bmp    = displayio.Bitmap(w, h, 4)
    pal    = displayio.Palette(4)
    pal[0] = 0xFF0000   # red
    pal[1] = 0x00FF00   # green
    pal[2] = 0x0000FF   # blue
    pal[3] = 0xFFFFFF   # white
    for y in range(h):
        for x in range(w):
            bmp[x, y] = x // bar_w if x < bar_w * 4 else 3

    grp = displayio.Group()
    grp.append(displayio.TileGrid(bmp, pixel_shader=pal))

    lbl = label.Label(
        terminalio.FONT,
        text=f"{w}x{h}",
        color=0xFFFFFF,
        x=4, y=h - 10
    )
    grp.append(lbl)
    display.root_group = grp
    time.sleep(3)


# =============================================================================
# Main
# =============================================================================

def list_candidates():
    print("\nKnown ST7735 configurations:\n")
    for i, c in enumerate(CANDIDATES):
        print(f"  {i + 1:2d}. {c['name']}")
        print(f"       {c['width']}x{c['height']}  rotation={c['rotation']}  "
              f"bgr={c['bgr']}  rowstart={c['rowstart']}  colstart={c['colstart']}")
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
                print(f"  [ERROR] Pattern failed: {e}")
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
        print("  - Image shifted/cropped → try rowstart/colstart variants")
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
