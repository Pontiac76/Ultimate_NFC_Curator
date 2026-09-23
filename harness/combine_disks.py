#!/usr/bin/env python3
r"""Combine one or more CBM disk images into a larger VICE image using c1541.

Examples:
  python combine_disks.py --source "R:\Hot Wheels (Epyx)*.d64" --target "R:\Hot Wheels (Epyx).d81"
  python combine_disks.py --source side1.d64 side2.d64 --target combined.d71
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Logical capacity ranking.  This is used only to prevent accidental down-converts.
IMAGE_RANK = {
    ".d64": 1,
    ".g64": 1,
    ".x64": 1,
    ".d71": 2,
    ".d81": 3,
    ".d80": 3,
    ".d82": 4,
}

FORMAT_TYPE = {
    ".d64": "d64",
    ".d71": "d71",
    ".d81": "d81",
    ".d80": "d80",
    ".d82": "d82",
}

HEADER_RE = re.compile(r'^\s*\d+\s+"([^"]*)"\s+(\S+)')
FILE_RE = re.compile(r'^\s*\d+\s+"([^"]*)"\s+(del|seq|prg|usr|rel)\b', re.I)


def default_c1541() -> str:
    env = os.environ.get("C1541")
    if env:
        return env
    found = shutil.which("c1541") or shutil.which("c1541.exe")
    if found:
        return found
    # Convenient default for the machine this was first written on; override with --c1541 elsewhere.
    return r"D:\WinVice\GTK3VICE-3.9-win64\bin\c1541.exe"


def run(args: list[str], cwd: Path | None = None, quiet: bool = False) -> str:
    if not quiet:
        print("+", " ".join(args))
    p = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if p.stdout and not quiet:
        print(p.stdout, end="")
    if p.stderr and not quiet:
        print(p.stderr, end="", file=sys.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"command failed with exit code {p.returncode}: {' '.join(args)}")
    return p.stdout


def c1541_quote(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def run_c1541_script(c1541: str, image: Path, commands: list[str], quiet: bool = False) -> str:
    script = "\n".join(commands + ["quit", ""])
    args = [c1541, str(image)]
    if not quiet:
        print("+", " ".join(args), "< script")
        for cmd in commands:
            print("  ", cmd)
    p = subprocess.run(args, input=script, text=True, capture_output=True)
    if p.stdout and not quiet:
        print(p.stdout, end="")
    if p.stderr and not quiet:
        print(p.stderr, end="", file=sys.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"c1541 script failed ({p.returncode}) on {image}")
    return p.stdout


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expand_sources(patterns: list[str]) -> list[Path]:
    sources: list[Path] = []
    for pat in patterns:
        matches = glob.glob(pat)
        if matches:
            sources.extend(Path(m) for m in matches)
        else:
            p = Path(pat)
            if p.exists():
                sources.append(p)
            else:
                raise FileNotFoundError(f"source matched no files: {pat}")
    # Wildcard results are intentionally case-insensitive sorted for deterministic disk order.
    unique = {str(p.resolve()).casefold(): p for p in sources}
    return sorted(unique.values(), key=lambda p: str(p).casefold())


def image_rank(path: Path) -> int:
    ext = path.suffix.lower()
    if ext not in IMAGE_RANK:
        raise ValueError(f"unsupported image extension: {path}")
    return IMAGE_RANK[ext]


def directory_listing(c1541: str, image: Path) -> tuple[str | None, str | None, list[str]]:
    out = run([c1541, str(image), "-list"], quiet=True)
    title = disk_id = None
    names: list[str] = []
    for line in out.splitlines():
        h = HEADER_RE.match(line)
        if h and title is None:
            title = h.group(1).rstrip()
            disk_id = h.group(2)[:2]
            continue
        m = FILE_RE.match(line)
        if m:
            names.append(m.group(1))
    return title, disk_id, names


def directory_order(c1541: str, image: Path) -> list[str]:
    return directory_listing(c1541, image)[2]


def extract_disk(c1541: str, image: Path, dest: Path) -> list[tuple[Path, str]]:
    dest.mkdir(parents=True, exist_ok=True)
    order = directory_order(c1541, image)
    run([c1541, str(image), "-extract"], cwd=dest)

    # c1541 may trim trailing spaces in host filenames.  Match back to directory order.
    host_files = {p.name.lower(): p for p in dest.iterdir() if p.is_file()}
    extracted: list[tuple[Path, str]] = []
    for name in order:
        host = host_files.get(name.lower()) or host_files.get(name.rstrip().lower())
        if host:
            extracted.append((host, name))
        else:
            print(f"Not extracted/skipping directory entry from {image.name}: {name!r}")
    return extracted


def unique_name(name: str, used: set[str], index: int) -> str:
    base = name[:13]
    for suffix in (f".{index}", ".2", ".3", ".4", ".5", ".6", ".7", ".8", ".9"):
        candidate = (base + suffix)[:16]
        if candidate.lower() not in used:
            return candidate
    raise RuntimeError(f"could not create unique 16-char name for duplicate {name!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Combine CBM disk images into a larger VICE image using c1541.")
    ap.add_argument("--source", "-s", nargs="+", required=True, help="Source image(s), may include wildcards")
    ap.add_argument("--target", "-t", required=True, help="Target image; type is determined by extension (.d71/.d81/etc.)")
    ap.add_argument("--c1541", default=default_c1541(), help="Path to c1541.exe, or set C1541 env var")
    ap.add_argument("--label", default=None, help='Disk label,id, e.g. "hot wheels,hw". Default uses target stem plus ,01')
    ap.add_argument("--duplicates", choices=("keep-first", "rename", "error"), default="keep-first")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite target if it exists")
    args = ap.parse_args()

    c1541 = args.c1541
    if not Path(c1541).exists() and not shutil.which(c1541):
        raise FileNotFoundError(f"c1541 not found: {c1541}")

    sources = expand_sources(args.source)
    target = Path(args.target)
    target_ext = target.suffix.lower()
    if target_ext not in FORMAT_TYPE:
        raise ValueError(f"target extension must be one of: {', '.join(sorted(FORMAT_TYPE))}")

    max_source_rank = max(image_rank(p) for p in sources)
    if IMAGE_RANK[target_ext] <= max_source_rank:
        raise ValueError("target must be a larger image type than all sources")

    if target.exists() and not args.overwrite:
        raise FileExistsError(f"target exists; use --overwrite: {target}")

    if args.label:
        label = args.label
    else:
        src_title, src_id, _ = directory_listing(c1541, sources[0])
        label = f"{(src_title or target.stem)[:16]},{(src_id or '01')[:2]}"

    print("Sources, in processing order:")
    for p in sources:
        print(f"  {p}")
    print(f"Target: {target}")

    with tempfile.TemporaryDirectory(prefix="combine_disks_") as td:
        tmp = Path(td)
        pending: list[tuple[int, Path, str]] = []
        for idx, image in enumerate(sources, start=1):
            extracted = extract_disk(c1541, image, tmp / f"src{idx}")
            print(f"Extracted {len(extracted)} files from {image.name}")
            pending.extend((idx, host, c64_name) for host, c64_name in extracted)

        if target.exists():
            target.unlink()
        run([c1541, "-format", label, FORMAT_TYPE[target_ext], str(target)])

        used: set[str] = set()
        hashes: dict[str, str] = {}
        written = skipped = renamed = 0

        for idx, host_file, c64_name in pending:
            key = c64_name.lower()
            out_name = c64_name
            h = digest(host_file)
            if key in used:
                if hashes.get(key) == h and args.duplicates != "error":
                    print(f"Skipping identical duplicate from source {idx}: {c64_name}")
                    skipped += 1
                    continue
                if args.duplicates == "error":
                    raise RuntimeError(f"duplicate C64 filename: {c64_name}")
                if args.duplicates == "keep-first":
                    print(f"Skipping conflicting duplicate from source {idx}: {c64_name}")
                    skipped += 1
                    continue
                out_name = unique_name(c64_name, used, idx)
                print(f"Renaming duplicate from source {idx}: {c64_name} -> {out_name}")
                renamed += 1

            # Use interactive command quoting so CBM names beginning with '-'
            # are treated as filenames, not c1541 command-line switches.
            run_c1541_script(c1541, target, [f"write {c1541_quote(str(host_file))} {c1541_quote(out_name)}"])
            used.add(out_name.lower())
            hashes[out_name.lower()] = h
            written += 1

    print(f"\nCreated: {target}")
    print(f"Written: {written}, skipped duplicates: {skipped}, renamed: {renamed}")
    run([c1541, str(target), "-list"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
