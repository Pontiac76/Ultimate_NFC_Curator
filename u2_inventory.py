#!/usr/bin/env python3
"""Build a shallow-ish inventory of launchable C64 files from the Ultimate-II+ USB."""

import argparse
import os
from pathlib import Path, PurePosixPath
from u2_common import *

DEFAULT_EXTS = (".d64", ".d71", ".d81", ".prg", ".crt", ".g64", ".tap")


def nonnegative_int(value):
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return parsed


def walk_ftp(host, root, max_depth, exts=DEFAULT_EXTS, progress_depth=3):
    todo = [(root.rstrip("/"), 0)]
    while todo:
        path, depth = todo.pop(0)
        try:
            if depth <= progress_depth:
                print(f"Scanning directory depth {depth}: {path}")
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
                if r["name"].lower().endswith(exts):
                    yield child, r


def suggest_for_file(host, path, inspect_disks=False):
    lower = path.lower()
    title = title_from_path(path)
    mode = "prg"
    entry = ""
    detail = ""
    if lower.endswith(".crt"):
        return {"title": title, "path": path, "file_type": "crt", "mode": "crt", "entry": "", "payload": payload_for("crt", path), "detail": "CRT"}
    if lower.endswith(".prg"):
        return {"title": title, "path": path, "file_type": "prg", "mode": "prg", "entry": "", "payload": payload_for("prg", path), "detail": "PRG"}
    if lower.endswith((".g64", ".tap")):
        file_type = lower.rsplit(".", 1)[1]
        mode = "tap" if file_type == "tap" else "disk"
        return {"title": title, "path": path, "file_type": file_type, "mode": mode, "entry": "", "payload": payload_for(mode, path), "detail": f"{file_type.upper()} image (unsupported/disabled by default)"}
    if lower.endswith((".d64", ".d71", ".d81")):
        file_type = lower.rsplit(".", 1)[1]
        mode = "disk"
        detail = file_type.upper()
        if inspect_disks:
            try:
                img = ftp_download(host, path)
                info = c1541_directory(img, suffix=f".{file_type}")
                prgs = [f for f in info["entries"] if f["type"] == "PRG"]
                label = f'{file_type.upper()} "{info.get("disk_name") or ""}"'.strip()
                if len(prgs) == 1:
                    mode = "prg"
                    entry = prgs[0]["name"]
                    detail = f"{label}; single PRG: {entry}"
                elif prgs:
                    mode = "disk"
                    entry = ""
                    detail = f"{label}; PRGs: " + ", ".join(f["name"] for f in prgs[:8])
                else:
                    mode = "disk"
                    detail = f"{label}; no PRGs found"
            except Exception as e:
                mode = "disk"
                detail = f"{file_type.upper()} inspect failed: {e}"
        return {"title": title, "path": path, "file_type": file_type, "mode": mode, "entry": entry, "payload": payload_for(mode, path, entry), "detail": detail}
    return None


def normalize_inventory_row(row):
    """Use curator candidate field names and USB-abstracted paths while preserving curation fields."""
    path = strip_usb_prefix(row.get("path", ""))
    typ = row.get("type") or row.get("file_type") or ""
    mode = row.get("mode", "")
    entry = row.get("entry", "")
    out = dict(row)
    out.update({
        "title": row.get("title", ""),
        "path": path,
        "type": typ,
        "file_type": typ,
        "mode": mode,
        "entry": entry,
        "payload": payload_for(mode, path, entry) if mode and path else row.get("payload", ""),
        "detail": row.get("detail", ""),
        "machine_mode": row.get("machine_mode") or "",
        "storage_status": row.get("storage_status") or "present",
    })
    return out


def merge_inventory_rows(existing, scanned):
    """Append only new exact normalized paths; preserve existing curated rows."""
    base_fields = ["title", "path", "type", "file_type", "mode", "entry", "payload", "detail", "machine_mode", "status", "storage_status", "notes", "quarantined", "quarantine_reason", "deleted_reason"]
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
    fields = list(dict.fromkeys(base_fields + [k for r in merged for k in r.keys()]))
    return merged, fields, added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--max-depth", type=nonnegative_int, default=4)
    ap.add_argument("--out", default="curator.db")
    ap.add_argument("--replace", action="store_true", help="replace output instead of appending new exact paths")
    ap.add_argument("--dir", dest="scan_dir", help="scan only this Ultimate directory, non-recursive unless --max-depth is also supplied")
    ap.add_argument("--selected-path", help="scan only the directory containing this selected Ultimate image path")
    ap.add_argument("--inspect-disks", action="store_true", help="download D64/D71/D81 images and inspect directories with c1541; slower")
    ap.add_argument("--progress-depth", type=nonnegative_int, default=3, help="print directory traversal progress through this depth; default 3")
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
    exts = tuple(f".{name}" for name in file_type_names(args.out))
    enabled_exts = tuple(f".{name}" for name in enabled_file_type_names(args.out))
    print(f"Known file types imported: {', '.join(exts)}")
    print(f"Enabled/displayed by default: {', '.join(enabled_exts)}")

    paths = [path for path, _ in walk_ftp(host, args.root, args.max_depth, exts, progress_depth=args.progress_depth)]
    if is_sqlite_path(args.out):
        import_scan_paths(args.out, paths)
        scan_stats = reconcile_scan_paths(args.out)
        print(
            "ScanPath: {scan_paths} scanned; {new_paths} new; {missing_images} missing; {returned_images} returned".format(**scan_stats)
        )

    scanned = []
    for path in paths:
        print(f"Inspecting {path}")
        row = suggest_for_file(host, path, inspect_disks=args.inspect_disks)
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
