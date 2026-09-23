#!/usr/bin/env python3
"""Test the new cold-boot/mount/load/run launch flow.

Examples:
  ./harness/u2_boot_launch.py /C64/Games/Ghostbusters/ghostbusters.d64 --mode c64
  ./harness/u2_boot_launch.py /C128/Games/128-Robots.d71 --mode c128
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import u2_common
from u2_common import boot_mount_load_run, resolve_ultimate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--mode", default="c64", choices=["c64", "c128", "64", "128"])
    ap.add_argument("--entry", default="", help="LOAD target for disk mode; blank means *")
    ap.add_argument("--skip-prehelp", action="store_true", help="Bypass pre-run help and send RUN normally")
    args = ap.parse_args()
    args.ultimate = resolve_ultimate(args.ultimate)
    status, body = boot_mount_load_run(args.ultimate, args.image, target_mode=args.mode, entry=args.entry, skip_prehelp=args.skip_prehelp)
    print(f"HTTP {status}")
    print(body)


if __name__ == "__main__":
    main()
