#!/usr/bin/env python3
"""Dump current C64/C128 40x25 text screen via Ultimate readmem as ANSI-ish text.

For C64 mode, screen base is detected from VIC-II registers $D018 and $DD00.
Color RAM is $D800. This is read-only.
"""

import argparse
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from u2_common import resolve_ultimate

ANSI_COLORS = [
    30, 37, 31, 36, 35, 32, 34, 33,
    90, 37, 91, 96, 95, 92, 94, 93,
]

# Rough C64 screen-code to Unicode mapping for readable dumps.
def screen_char(c, lowercase_charset=False):
    c &= 0x7f
    if c == 0x00: return '@'
    if 1 <= c <= 26:
        base = ord('a') if lowercase_charset else ord('A')
        return chr(base + c - 1)
    if 27 <= c <= 31: return '?'
    if c == 32: return ' '
    if 33 <= c <= 63: return chr(c)
    if c == 64: return '─'
    if 65 <= c <= 90:
        base = ord('A') if lowercase_charset else ord('a')
        return chr(base + c - 65)
    if 91 <= c <= 95: return '?'
    if c == 96: return '─'
    if 97 <= c <= 122:
        base = ord('a') if lowercase_charset else ord('A')
        return chr(base + c - 97)
    return '?'


def readmem(host, address, length):
    url = f"http://{host}/v1/machine:readmem?" + urllib.parse.urlencode({
        "address": f"{address:04x}",
        "length": str(length),
    })
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--screen", default="auto", help="screen base hex, or auto")
    ap.add_argument("--charset", default="auto", choices=["auto", "upper", "lower"], help="C64 character set/case mapping")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--out", default="screen_dump_ansi.txt")
    args = ap.parse_args()

    args.ultimate = resolve_ultimate(args.ultimate)

    if args.screen == "auto":
        d018 = readmem(args.ultimate, 0xD018, 1)[0]
        dd00 = readmem(args.ultimate, 0xDD00, 1)[0]
        vic_bank = (3 - (dd00 & 0x03)) * 0x4000
        screen_base = vic_bank + ((d018 >> 4) & 0x0f) * 0x0400
        lowercase_charset = bool(d018 & 0x02)
        print(f"D018=${d018:02x} DD00=${dd00:02x} VIC bank=${vic_bank:04x} screen=${screen_base:04x} charset={'lower' if lowercase_charset else 'upper'}")
    else:
        screen_base = int(args.screen, 16)
        print(f"screen=${screen_base:04x}")
        lowercase_charset = False

    if args.charset == "lower":
        lowercase_charset = True
    elif args.charset == "upper":
        lowercase_charset = False

    screen = readmem(args.ultimate, screen_base, 1000)
    colors = bytes([1] * 1000) if args.no_color else readmem(args.ultimate, 0xD800, 1000)

    lines = []
    for y in range(25):
        line = []
        last_ansi = None
        for x in range(40):
            i = y * 40 + x
            fg = colors[i] & 0x0f
            ansi = ANSI_COLORS[fg]
            if ansi != last_ansi:
                line.append(f"\x1b[{ansi}m")
                last_ansi = ansi
            ch = screen_char(screen[i], lowercase_charset)
            if screen[i] & 0x80:
                line.append("\x1b[7m" + ch + f"\x1b[0m\x1b[{ansi}m")
            else:
                line.append(ch)
        line.append("\x1b[0m")
        lines.append("".join(line))

    text = "\n".join(lines) + "\n"
    with open(args.out, "w") as f:
        f.write(text)
    print(f"wrote {args.out}")
    print(text)


if __name__ == "__main__":
    main()
