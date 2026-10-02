#!/usr/bin/env python3
"""Probe the Assembly64/Ultimate search API.

Discovery source: Ultimate firmware software/network/assembly.cc
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://hackerswithstyle.se"
HEADERS = {
    "Accept-encoding": "identity",
    "User-Agent": "Assembly Query",
    "Client-Id": "Ultimate",
}


def fetch(path, binary=False):
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        if binary:
            return r, data
        text = data.decode("utf-8", "replace")
        try:
            return r, json.loads(text)
        except json.JSONDecodeError:
            return r, text


def dump(obj):
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(obj)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("presets")
    s = sub.add_parser("search")
    s.add_argument("query", help='AQL query, e.g. (name:"jumpman") & (type:prg)')
    e = sub.add_parser("entries")
    e.add_argument("id")
    e.add_argument("category", type=int)
    d = sub.add_parser("download")
    d.add_argument("id")
    d.add_argument("category", type=int)
    d.add_argument("index", type=int)
    d.add_argument("--out")
    args = ap.parse_args()

    if args.cmd == "presets":
        r, obj = fetch("/leet/search/aql/presets")
        print(r.status, r.geturl())
        dump(obj)
    elif args.cmd == "search":
        q = urllib.parse.quote(args.query)
        r, obj = fetch("/leet/search/aql?query=" + q)
        print(r.status, r.geturl())
        dump(obj)
    elif args.cmd == "entries":
        eid = urllib.parse.quote(args.id)
        r, obj = fetch(f"/leet/search/entries/{eid}/{args.category}")
        print(r.status, r.geturl())
        dump(obj)
    elif args.cmd == "download":
        eid = urllib.parse.quote(args.id)
        path = f"/leet/search/bin/{eid}/{args.category}/{args.index}"
        r, data = fetch(path, binary=True)
        print(r.status, r.geturl(), r.headers.get("content-type"), len(data), file=sys.stderr)
        out = args.out or f"assembly64_{args.id}_{args.category}_{args.index}.bin"
        with open(out, "wb") as f:
            f.write(data)
        print(out)


if __name__ == "__main__":
    main()
