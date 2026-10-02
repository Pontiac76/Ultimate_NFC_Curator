#!/usr/bin/env python3
"""Local image conversion helpers for curator workflows.

This module intentionally knows nothing about curses, Assembly64 database rows,
or TUI state. It converts local files and returns the resulting local path. The
caller decides how to present progress and how to record the new candidate.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from u2_common import c1541_available, c1541_directory_from_file

DEFAULT_CONVERSION_OUTPUT_DIR = Path("a64_inbox")


def safe_disk_title(title: str) -> str:
    """Return a c1541-friendly disk title fragment."""
    text = re.sub(r"[^A-Za-z0-9 _.-]+", "_", str(title or "")).strip(" ._")
    text = re.sub(r"\s+", "_", text)
    return text or "A64_Image"


def conversion_targets(file_type: str) -> list[str]:
    return {
        "prg": ["d64", "d71", "d81"],
        "d64": ["d71", "d81"],
        "d71": ["d81"],
    }.get(str(file_type).lower().lstrip("."), [])


def create_local_prg_image(local_prg, target_ext, title, output_dir=DEFAULT_CONVERSION_OUTPUT_DIR):
    if not c1541_available():
        raise RuntimeError("c1541 is not installed")
    local_prg = Path(local_prg)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"{local_prg.stem}.converted.{target_ext}"
    tmp = tempfile.NamedTemporaryFile(suffix=f".{target_ext}", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    disk_type = str(target_ext).lower()
    disk_title = safe_disk_title(title)[:16] or "a64"
    try:
        r = subprocess.run(
            ["c1541", "-format", f"{disk_title},64", disk_type, tmp.name, "-write", str(local_prg), local_prg.stem[:16]],
            text=True,
            capture_output=True,
            timeout=60,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "c1541 conversion failed").strip())
        shutil.move(tmp.name, out)
        return out
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass


def convert_local_disk_image(local_disk, target_ext, title, output_dir=DEFAULT_CONVERSION_OUTPUT_DIR):
    if not c1541_available():
        raise RuntimeError("c1541 is not installed")
    local_disk = Path(local_disk)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    src_info = c1541_directory_from_file(local_disk)
    out = output_dir / f"{local_disk.stem}.converted.{target_ext}"
    tmpdir = Path(tempfile.mkdtemp(prefix="a64_convert_"))
    tmpout = tempfile.NamedTemporaryFile(suffix=f".{target_ext}", delete=False)
    tmpout.close()
    os.unlink(tmpout.name)
    try:
        disk_title = safe_disk_title(src_info.get("disk_name") or title)[:16] or "a64"
        r = subprocess.run(["c1541", "-format", f"{disk_title},64", target_ext, tmpout.name], text=True, capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "c1541 format failed").strip())
        for e in src_info.get("entries", []):
            if e.get("type") != "PRG":
                continue
            name = e.get("name", "")
            host_file = tmpdir / re.sub(r"[^A-Za-z0-9._-]+", "_", name or "entry.prg")
            r = subprocess.run(["c1541", str(local_disk), "-read", name, str(host_file)], text=True, capture_output=True, timeout=60)
            if r.returncode != 0 or not host_file.exists():
                continue
            r = subprocess.run(["c1541", tmpout.name, "-write", str(host_file), name], text=True, capture_output=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or f"c1541 write failed for {name}").strip())
        shutil.move(tmpout.name, out)
        return out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            os.unlink(tmpout.name)
        except FileNotFoundError:
            pass


def run_local_conversion_action(local, row, target, output_dir=DEFAULT_CONVERSION_OUTPUT_DIR):
    ft = Path(local).suffix.lower().lstrip(".")
    title = row.get("title") or Path(local).stem
    if ft == "prg":
        return create_local_prg_image(local, target, title, output_dir=output_dir)
    if ft in ("d64", "d71"):
        return convert_local_disk_image(local, target, title, output_dir=output_dir)
    raise RuntimeError(f"{ft.upper()} conversion reserved")
