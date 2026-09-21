#!/usr/bin/env python3
"""Build a shallow-ish inventory of launchable C64 files from the Ultimate-II+ USB."""

import argparse
import os
from pathlib import Path, PurePosixPath
from u2_common import *

EXTS = (".d64", ".d71", ".d81", ".prg", ".crt")


def walk_ftp(host, root, max_depth):
    todo = [(root.rstrip("/"), 0)]
    while todo:
        path, depth = todo.pop(0)
        try:
            rows = parse_ftp_list(ftp_list(host, path))
        except Exception as e:
            print(f"WARN: cannot list {path}: {e}")
            continue
        for r in rows:
            child = path + "/" + r["name"]
            if r["type"] == "dir":
                if depth < max_depth:
                    todo.append((child, depth + 1))
            else:
                if r["name"].lower().endswith(EXTS):
                    yield child, r


def suggest_for_file(host, path):
    lower = path.lower()
    title = title_from_path(path)
    mode = "prg"
    entry = ""
    detail = ""
    if lower.endswith(".crt"):
        return {"title": title, "path": path, "file_type": "crt", "mode": "crt", "entry": "", "payload": payload_for("crt", path), "detail": "CRT"}
    if lower.endswith(".prg"):
        return {"title": title, "path": path, "file_type": "prg", "mode": "prg", "entry": "", "payload": payload_for("prg", path), "detail": "PRG"}
    if lower.endswith((".d71", ".d81")):
        file_type = lower.rsplit(".", 1)[1]
        return {"title": title, "path": path, "file_type": file_type, "mode": "disk", "entry": "", "payload": payload_for("disk", path), "detail": file_type.upper()}
    if lower.endswith(".d64"):
        file_type = "d64"
        try:
            img = ftp_download(host, path)
            prgs = [f for f in d64_directory(img) if f["type"] == "PRG"]
            if len(prgs) == 1:
                mode = "prg"
                entry = prgs[0]["name"]
                detail = f"single PRG: {entry}"
            elif prgs:
                mode = "disk"
                # For disk mode, blank entry means LOAD"*",8,1. Do not guess
                # the first PRG as the loader; curator/user should opt into a
                # named LOAD target explicitly.
                entry = ""
                detail = "PRGs: " + ", ".join(f["name"] for f in prgs[:8])
            else:
                mode = "disk"
                detail = "no PRGs found"
        except Exception as e:
            file_type = "d64"
            mode = "disk"
            detail = f"D64 inspect failed: {e}"
        return {"title": title, "path": path, "file_type": file_type, "mode": mode, "entry": entry, "payload": payload_for(mode, path, entry), "detail": detail}
    return None


def normalize_inventory_row(row):
    """Use curator candidate field names and USB-abstracted paths."""
    path = strip_usb_prefix(row.get("path", ""))
    typ = row.get("type") or row.get("file_type") or ""
    mode = row.get("mode", "")
    entry = row.get("entry", "")
    return {
        "title": row.get("title", ""),
        "path": path,
        "type": typ,
        "mode": mode,
        "entry": entry,
        "payload": payload_for(mode, path, entry) if mode and path else row.get("payload", ""),
        "detail": row.get("detail", ""),
    }


def merge_inventory_rows(existing, scanned):
    """Append only new exact normalized paths; preserve existing curated rows."""
    fields = ["title", "path", "type", "mode", "entry", "payload", "detail"]
    merged = [normalize_inventory_row(r) for r in existing]
    seen = {strip_usb_prefix(r.get("path", "")) for r in merged if r.get("path")}
    added = 0
    for row in scanned:
        nr = normalize_inventory_row(row)
        key = strip_usb_prefix(nr.get("path", ""))
        if not key or key in seen:
            continue
        merged.append(nr)
        seen.add(key)
        added += 1
    return merged, fields, added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--max-depth", type=int, default=4)
    ap.add_argument("--out", default="rest_key_game_candidates.tsv")
    ap.add_argument("--replace", action="store_true", help="replace output instead of appending new exact paths")
    ap.add_argument("--dir", dest="scan_dir", help="scan only this Ultimate directory, non-recursive unless --max-depth is also supplied")
    ap.add_argument("--selected-path", help="scan only the directory containing this selected Ultimate image path")
    args = ap.parse_args()

    if args.selected_path:
        selected = strip_usb_prefix(args.selected_path)
        args.root = str(PurePosixPath(selected).parent)
        args.max_depth = 0
        print(f"Scanning directory for selected path: {args.root}")
    elif args.scan_dir:
        args.root = strip_usb_prefix(args.scan_dir)
        # --dir is intentionally non-recursive by convention.
        args.max_depth = 0
        print(f"Scanning single directory: {args.root}")

    host = discover_ultimate() if args.ultimate == "auto" else args.ultimate
    if not host:
        raise SystemExit(1)
    print(f"Inventorying {host}:{args.root}")

    scanned = []
    for path, _ in walk_ftp(host, args.root, args.max_depth):
        print(f"Inspecting {path}")
        row = suggest_for_file(host, path)
        if row:
            scanned.append(row)

    fields = ["title", "path", "type", "mode", "entry", "payload", "detail"]
    if Path(args.out).exists() and not args.replace:
        existing = read_csv(args.out)
        rows, fields, added = merge_inventory_rows(existing, scanned)
        write_csv(args.out, rows, fields)
        print(f"Scanned {len(scanned)} row(s); appended {added} new row(s) to {args.out}; preserved {len(existing)} existing row(s)")
    else:
        rows = [normalize_inventory_row(r) for r in scanned]
        write_csv(args.out, rows, fields)
        print(f"Wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
