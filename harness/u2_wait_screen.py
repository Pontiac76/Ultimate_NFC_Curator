#!/usr/bin/env python3
"""Wait for text to appear on the C64/C128 text screen."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from u2_common import read_text_screen, wait_for_screen_text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", help="text to wait for; omit to just dump screen")
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--exact", action="store_true", help="exact case-insensitive search; default is generalized")
    args = ap.parse_args()
    if args.ultimate == "auto":
        from u2_common import discover_ultimate
        args.ultimate = discover_ultimate()
    if args.text:
        ok = wait_for_screen_text(args.ultimate, args.text, timeout=args.timeout, exact=args.exact)
        raise SystemExit(0 if ok else 1)
    text, info = read_text_screen(args.ultimate)
    print(info)
    print(text)


if __name__ == "__main__":
    main()
