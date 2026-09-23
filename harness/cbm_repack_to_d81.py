#!/usr/bin/env python3
"""Repack a normal CBM DOS disk image into a D81, preserving directory order.

This is intended for already-cracked/unprotected disks whose files can be copied
through the filesystem. It will not preserve copy protection, custom sectors,
loader-specific sector placement, or nonstandard disk layouts.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from u2_common import ftp_download, resolve_ultimate, strip_usb_prefix  # noqa:E402


DIR_HEADER_RE = re.compile(r'^\s*\d+\s+"(?P<title>[^"]*)"\s+(?P<id>\S+)')
DIR_ENTRY_RE = re.compile(r'^\s*(?P<blocks>\d+)\s+"(?P<name>[^"]+)"\s+(?P<type>[A-Z]{3})(?:\s|$)', re.I)


def run_c1541(args, cwd=None):
    cmd = ["c1541", *args]
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"c1541 failed ({p.returncode}): {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )
    return p.stdout


def safe_host_name(index, cbm_name):
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", cbm_name).strip() or "file"
    return f"{index:03d}_{name}"


def parse_listing(text):
    title = "repacked"
    disk_id = "01"
    files = []
    for line in text.splitlines():
        m = DIR_HEADER_RE.match(line)
        if m:
            title = m.group("title").rstrip() or title
            disk_id = m.group("id")[:2] or disk_id
            continue
        m = DIR_ENTRY_RE.match(line)
        if m:
            typ = m.group("type").upper()
            if typ == "DEL":
                continue
            files.append({
                "blocks": int(m.group("blocks")),
                "name": m.group("name"),
                "type": typ,
            })
    return title, disk_id, files


def source_to_local(source, host):
    p = Path(source)
    if p.exists():
        return p, None
    # Treat non-local absolute-ish paths as Ultimate paths.
    if source.startswith("U2+:"):
        source = source[4:]
    data = ftp_download(host, source)
    suffix = Path(strip_usb_prefix(source)).suffix or ".d64"
    tmp = tempfile.NamedTemporaryFile(prefix="u2_src_", suffix=suffix, delete=False)
    tmp.write(data)
    tmp.close()
    return Path(tmp.name), Path(tmp.name)


def main():
    ap = argparse.ArgumentParser(description="Repack D64/D71/D81 files into a new D81 preserving directory order")
    ap.add_argument("source", help="local image path or Ultimate path like /d64/foo.d64")
    ap.add_argument("output", help="output D81 image path")
    ap.add_argument("--ultimate", default="auto", help="Ultimate host for non-local source paths")
    ap.add_argument("--work", help="keep/use work directory for extracted files")
    ap.add_argument("--title", help="override output disk title")
    ap.add_argument("--id", dest="disk_id", help="override output disk id")
    ap.add_argument("--force", action="store_true", help="overwrite output if it exists")
    ap.add_argument("--dry-run", action="store_true", help="show planned operations without creating output")
    args = ap.parse_args()

    host = resolve_ultimate(args.ultimate) if (args.source.startswith("/") or args.source.startswith("U2+:")) and not Path(args.source).exists() else None
    source_local, temp_source = source_to_local(args.source, host) if host else (Path(args.source), None)
    if not source_local.exists():
        raise SystemExit(f"Source image not found: {args.source}")

    out = Path(args.output)
    if out.exists() and not args.force and not args.dry_run:
        raise SystemExit(f"Output exists; use --force: {out}")

    listing = run_c1541([str(source_local), "-list"])
    title, disk_id, files = parse_listing(listing)
    title = (args.title or title)[:16]
    disk_id = (args.disk_id or disk_id or "01")[:2]

    print(f"Source: {source_local}")
    print(f"Output: {out}")
    print(f"Format: d81 title={title!r} id={disk_id!r}")
    print("Files in directory order:")
    for i, f in enumerate(files, 1):
        print(f"  {i:3d}. {f['name']!r} {f['type']} {f['blocks']} blocks")

    if args.dry_run:
        return

    if out.exists():
        out.unlink()

    if args.work:
        work = Path(args.work)
        work.mkdir(parents=True, exist_ok=True)
        cleanup_work = False
    else:
        work = Path(tempfile.mkdtemp(prefix="cbm_repack_"))
        cleanup_work = True

    manifest = work / "manifest.tsv"
    try:
        with manifest.open("w") as mf:
            mf.write("index\tblocks\ttype\tcbm_name\thost_file\n")
            for i, f in enumerate(files, 1):
                host_name = safe_host_name(i, f["name"])
                host_path = work / host_name
                print(f"Extract {i}/{len(files)}: {f['name']!r} -> {host_path.name}")
                run_c1541([str(source_local), "-read", f["name"], str(host_path)])
                mf.write(f"{i}\t{f['blocks']}\t{f['type']}\t{f['name']}\t{host_path.name}\n")

        print(f"Create D81: {out}")
        run_c1541(["-format", f"{title},{disk_id}", "d81", str(out)])

        for i, f in enumerate(files, 1):
            host_path = work / safe_host_name(i, f["name"])
            print(f"Write {i}/{len(files)}: {host_path.name} -> {f['name']!r}")
            run_c1541([str(out), "-write", str(host_path), f["name"]])

        print("\nNew image directory:")
        print(run_c1541([str(out), "-list"]))
        print(f"Manifest: {manifest}")
    finally:
        if cleanup_work:
            shutil.rmtree(work, ignore_errors=True)
        if temp_source:
            temp_source.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
