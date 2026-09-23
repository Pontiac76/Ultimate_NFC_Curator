#!/usr/bin/env python3
"""Inject ASCII text into the C128 keyboard buffer via Ultimate API."""

import argparse
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from u2_common import resolve_ultimate


def put(host, path, params=None):
    url = f"http://{host}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="PUT")
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, r.read().decode("utf-8", "replace")


def post_bytes(host, path, params, data):
    url = f"http://{host}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, r.read().decode("utf-8", "replace")


def inverse_ascii_case(text):
    return "".join(ch.lower() if ch.isupper() else ch.upper() if ch.islower() else ch for ch in text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="text to type; use --enter to append Return")
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--enter", action="store_true")
    ap.add_argument("--raw", action="store_true", help="do not invert ASCII case before writing keyboard buffer")
    ap.add_argument("--mode", default="c128", choices=["c128", "128", "c64", "64"], help="keyboard buffer to use")
    args = ap.parse_args()
    args.ultimate = resolve_ultimate(args.ultimate)
    text = args.text + ("\r" if args.enter else "")
    wire_text = text if args.raw else inverse_ascii_case(text)
    mode = "c128" if args.mode in ("c128", "128") else "c64"
    buf_addr, count_addr = ("034a", "00d0") if mode == "c128" else ("0277", "00c6")
    print(f"mode:     {mode}")
    print(f"buffer:   ${buf_addr} count=${count_addr}")
    print(f"intended: {text!r}")
    print(f"wire:     {wire_text!r}")
    data = wire_text.encode("ascii")
    status, body = post_bytes(args.ultimate, "/v1/machine:writemem", {"address": buf_addr}, data)
    print(f"write keyboard buffer: HTTP {status} {body.strip()}")
    status, body = put(args.ultimate, "/v1/machine:writemem", {"address": count_addr, "data": f"{len(data):02x}"})
    print(f"write keyboard count:  HTTP {status} {body.strip()}")


if __name__ == "__main__":
    main()
