#!/usr/bin/env python3
"""Guided writer for approved_games.csv. Uses nfc_write_text.py for each card."""

import argparse
import subprocess
import sys
from u2_common import *


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="?", default="approved_games.csv")
    ap.add_argument("--device", default="/dev/ttyUSB0")
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--skip-launch-test", action="store_true")
    ap.add_argument("--writer", default="./nfc_write_text.py")
    args = ap.parse_args()

    rows = read_csv(args.manifest)
    if not rows:
        raise SystemExit(f"No rows in {args.manifest}")
    host = None if args.skip_launch_test else (discover_ultimate() if args.ultimate == "auto" else args.ultimate)

    print(f"Loaded {len(rows)} approved games from {args.manifest}")
    print("Cards should be physically labeled in this same order. Ctrl-C to stop.\n")

    for i, r in enumerate(rows, 1):
        title = r.get("title", "")
        payload = r.get("payload", "")
        print("=" * 72)
        print(f"[{i}/{len(rows)}] {title}")
        print(payload)
        if not payload.startswith("U2+:"):
            print("Skipping: payload does not start with U2+:")
            continue

        if host and not args.skip_launch_test:
            ans = input("Test launch before writing? [Y/n] ").strip().lower()
            if ans in ("", "y", "yes"):
                try:
                    status, body = launch_payload(host, payload, d64_as_prg_loader=True)
                    print(f"Launch response HTTP {status}: {body.strip()}")
                except Exception as e:
                    print(f"Launch failed: {e}")
                    ans = input("Continue to write this card anyway? [y/N] ").strip().lower()
                    if ans not in ("y", "yes"):
                        continue
                ans = input("Did it look good on the 128? Write card now? [y/N] ").strip().lower()
                if ans not in ("y", "yes"):
                    continue

        input(f"Place the card labeled {title!r} on the reader, then press Enter...")
        cmd = [sys.executable, args.writer, "--device", args.device, "--yes", payload]
        try:
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError as e:
            print(f"WRITE FAILED for {title}: {e}")
            ans = input("Retry this same card? [y/N] ").strip().lower()
            if ans in ("y", "yes"):
                subprocess.check_call(cmd)
            else:
                continue

        print("Card written. Remove it from the reader.")
        input("Press Enter for next card...")

    print("Done.")


if __name__ == "__main__":
    main()
