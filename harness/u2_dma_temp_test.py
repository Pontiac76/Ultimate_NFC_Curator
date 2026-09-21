#!/usr/bin/env python3
"""DMA launch a single-file D64 PRG, reboot, then test /Temp cleanup."""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from u2_common import cold_boot, d64_single_prg, post_runner, resolve_ultimate
from harness.u2_clean_temp import clean_temp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?", default="/C64/Games/Ghostbusters/ghostbusters.d64")
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--launch-wait", type=float, default=3.0)
    ap.add_argument("--boot-wait", type=float, default=3.0)
    args = ap.parse_args()

    args.ultimate = resolve_ultimate(args.ultimate)
    print(f"Inspecting {args.image}")
    single = d64_single_prg(args.ultimate, args.image)
    if not single:
        raise SystemExit("Not a single-PRG D64")
    name, prg = single
    load = prg[0] + 256 * prg[1] if len(prg) >= 2 else 0
    print(f"DMA launch {name!r}, {len(prg)} bytes, load=${load:04x}")
    status, body = post_runner(args.ultimate, "/v1/runners:run_prg", prg)
    print(f"run_prg HTTP {status}: {body.strip()}")

    print(f"Waiting {args.launch_wait}s...")
    time.sleep(args.launch_wait)
    print("Cold boot/reset")
    cold_boot(args.ultimate)
    print(f"Waiting {args.boot_wait}s...")
    time.sleep(args.boot_wait)

    print("Cleaning /Temp")
    n = clean_temp(args.ultimate)
    print(f"Deleted {n} temp file(s)")


if __name__ == "__main__":
    main()
