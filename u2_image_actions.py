#!/usr/bin/env python3
"""Curated image action helpers.

Business operations for normal curator images live here. The curses UI remains
responsible for asking dangerous questions like "really delete this?" because
apparently users prefer warnings before vaporizing files. Sensible enough.
"""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from u2_common import ensure_rows_table, ftp_delete, ftp_download, ftp_upload, lookup_id, payload_for, sqlite_connect, strip_usb_prefix
from u2_a64_workflow import mode_for_ext
from u2_image_conversion import run_local_conversion_action


def mark_image_deleted(db_path, image_path: str, reason: str = "Deleted from TUI") -> None:
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        conn.execute(
            """
            UPDATE Image
            SET fk_StorageStatus_ID=?, deleted_reason=?, deleted_at=CURRENT_TIMESTAMP
            WHERE path=?
            """,
            (lookup_id(conn, "StorageStatus", "deleted"), reason, image_path),
        )


def delete_curated_image(host: str, db_path, image_path: str, reason: str = "Deleted from TUI") -> str:
    if not image_path:
        raise RuntimeError("Selected row has no path")
    try:
        deleted_path = ftp_delete(host, image_path)
    except Exception:
        # If the file is already gone, still mark the DB deleted. The curator's
        # job is to reflect intended library state, not reenact FTP court drama.
        deleted_path = image_path
    mark_image_deleted(db_path, image_path, reason=reason)
    return deleted_path


def remove_row_by_path(rows: list[dict[str, Any]], path: str) -> bool:
    for i, row in enumerate(list(rows)):
        if row.get("path") == path:
            del rows[i]
            return True
    return False


def converted_image_destination(source_path: str, target_ext: str) -> str:
    source = PurePosixPath(strip_usb_prefix(source_path))
    target_ext = target_ext.lower().lstrip(".")
    return str(source.with_name(f"{source.stem}.converted.{target_ext}"))


def record_curated_image(conn, row: dict[str, Any], image_path: str, title: str, ext: str, detail: str, notes: str) -> dict[str, Any]:
    mode = mode_for_ext(ext)
    payload = payload_for(mode, image_path, "")
    machine_mode = row.get("machine_mode") or "c64"
    vals = {
        "title": title,
        "path": image_path,
        "payload": payload,
        "entry": "",
        "detail": detail,
        "notes": notes,
        "quarantine_reason": "",
        "deleted_reason": "",
        "quarantined": 0,
        "fk_Status_ID": None,
        "fk_StorageStatus_ID": lookup_id(conn, "StorageStatus", "present"),
        "fk_MachineMode_ID": lookup_id(conn, "MachineMode", machine_mode),
        "fk_FileType_ID": lookup_id(conn, "FileType", ext),
        "fk_LaunchMode_ID": lookup_id(conn, "LaunchMode", mode),
    }
    cols = ", ".join(vals)
    placeholders = ", ".join("?" for _ in vals)
    updates = ", ".join(f"{k}=excluded.{k}" for k in vals if k != "path")
    conn.execute(
        f"INSERT INTO Image ({cols}) VALUES ({placeholders}) ON CONFLICT(path) DO UPDATE SET {updates}",
        list(vals.values()),
    )
    return {
        "title": title,
        "path": image_path,
        "payload": payload,
        "mode": mode,
        "entry": "",
        "machine_mode": machine_mode,
        "file_type": ext,
        "type": ext,
        "detail": detail,
        "status": "",
        "storage_status": "present",
        "notes": notes,
    }


def convert_curated_image(host: str, db_path, row: dict[str, Any], target_ext: str) -> tuple[str, dict[str, Any]]:
    source_path = row.get("path", "")
    if not source_path:
        raise RuntimeError("Selected row has no path")
    source_ext = Path(source_path).suffix.lower().lstrip(".")
    target_ext = target_ext.lower().lstrip(".")
    suffix = f".{source_ext or 'img'}"
    with tempfile.TemporaryDirectory(prefix="u2_convert_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        local_source = tmpdir_path / (PurePosixPath(source_path).name or f"source{suffix}")
        local_source.write_bytes(ftp_download(host, source_path))
        converted = run_local_conversion_action(local_source, row, target_ext, output_dir=tmpdir_path)
        destination = converted_image_destination(source_path, target_ext)
        uploaded = ftp_upload(host, destination, Path(converted).read_bytes())
    image_path = strip_usb_prefix(uploaded)
    title = f"{row.get('title') or PurePosixPath(source_path).stem} - Converted [to {target_ext.upper()}]"
    detail = f"Converted {source_ext.upper()} to {target_ext.upper()}"
    notes = f"Converted from {source_path}"
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        new_row = record_curated_image(conn, row, image_path, title, target_ext, detail, notes)
    return image_path, new_row
