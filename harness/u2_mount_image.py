#!/usr/bin/env python3
"""Mount a disk image into Ultimate drive a or b.

Examples:
  ./harness/u2_mount_image.py /C128/Games/128-Robots.d71
  ./harness/u2_mount_image.py /nibs/Elite.g64 --catalog
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from u2_common import candidate_usb_paths, save_state, resolve_ultimate


def put(host, path, params=None):
    url = f"http://{host}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {body}")


def post_bytes(host, path, params, data):
    url = f"http://{host}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, r.read().decode("utf-8", "replace")


def inject_c128_keys(host, text):
    # C128 keyboard buffer starts at $034A; count is $00D0. CR is \r.
    data = text.encode("ascii")
    status, body = post_bytes(host, "/v1/machine:writemem", {"address": "034a"}, data)
    print(f"write keyboard buffer: HTTP {status} {body.strip()}")
    status, body = put(host, "/v1/machine:writemem", {"address": "00d0", "data": f"{len(data):02x}"})
    print(f"write keyboard count:  HTTP {status} {body.strip()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="image path, with or without /Usb0 or /Usb1")
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--drive", default="a", choices=["a", "b"])
    ap.add_argument("--catalog", action="store_true", help="after mount, type CATALOG+Enter on the C128")
    ap.add_argument("--keys", default=None, help="custom ASCII keys to inject instead of CATALOG\\r")
    args = ap.parse_args()

    args.ultimate = resolve_ultimate(args.ultimate)
    last_err = None
    for concrete in candidate_usb_paths(args.image):
        try:
            status, body = put(args.ultimate, f"/v1/drives/{args.drive}:mount", {"image": concrete})
            print(f"mounted {concrete} on drive {args.drive}: HTTP {status}")
            print(body.strip())
            if concrete.startswith("/Usb0"):
                save_state(last_usb=0)
            elif concrete.startswith("/Usb1"):
                save_state(last_usb=1)
            if args.catalog or args.keys is not None:
                time.sleep(0.5)
                inject_c128_keys(args.ultimate, args.keys if args.keys is not None else "CATALOG\r")
            return
        except Exception as e:
            print(f"failed {concrete}: {e}")
            last_err = e
    raise SystemExit(f"all mount attempts failed: {last_err}")


if __name__ == "__main__":
    main()
