#!/usr/bin/env python3
"""Assembly64 curator workflow helpers.

This module holds A64 business/workflow logic that does not need to know about
curses screens. The TUI may ask questions and show progress; this module does
the less glamorous paperwork.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from u2_a64_client import DEFAULT_INBOX
from u2_a64_db import db_connect as a64_db_connect, upsert_entry as a64_upsert_entry, upsert_result as a64_upsert_result
from u2_common import (
    ensure_rows_table,
    ftp_upload,
    lookup_id,
    payload_for,
    sqlite_connect,
    strip_usb_prefix,
)


def is_a64_row(row: dict[str, Any]) -> bool:
    return bool(row.get("a64_id") and row.get("a64_category") is not None and row.get("entry_index") is not None)


def is_local_a64_row(row: dict[str, Any]) -> bool:
    p = str(row.get("path") or "")
    return is_a64_row(row) and p and not p.startswith("/") and Path(p).exists()


def normalize_a64_search_value(value) -> str:
    return str(value or "").strip().lower()


def safe_filename_title(title) -> str:
    s = re.sub(r"[^A-Za-z0-9 _.-]+", "_", str(title or "")).strip(" ._")
    s = re.sub(r"\s+", "_", s)
    return s or "A64_Image"


def a64_bucket(title) -> str:
    s = safe_filename_title(title)
    ch = s[0].upper() if s else "#"
    return ch if "A" <= ch <= "Z" else "#"


def suggested_a64_promote_path(row: dict[str, Any]) -> str:
    title = row.get("title") or Path(row.get("original_filename") or row.get("path", "A64_Image")).stem
    safe_title = safe_filename_title(title)
    a64_id = str(row.get("a64_id") or "a64")
    short_id = a64_id if len(a64_id) <= 12 else a64_id[:12]
    ext = Path(row.get("path") or row.get("original_filename") or "").suffix.lower() or ".bin"
    return f"/Usb0/A64/{a64_bucket(safe_title)}/{safe_title}-{short_id}{ext}"


def mode_for_ext(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    if ext in ("d64", "d71", "d81", "g64"):
        return "disk"
    if ext == "crt":
        return "crt"
    if ext == "sid":
        return "sid"
    if ext == "tap":
        return "tap"
    return "prg"


def update_a64_entry_state(db_path, row: dict[str, Any], state: str) -> bool:
    if not is_a64_row(row):
        return False
    # SQL identifiers cannot be parameterized. Keep this hardcoded mapping only;
    # do not accept column names from user input unless you enjoy preventable fires.
    col = {
        "failed": "failed_at",
        "deleted": "deleted_at",
        "discarded": "discarded_at",
        "promoted": "promoted_at",
        "tested": "tested_at",
    }.get(state)
    if not col:
        return False
    with a64_db_connect(db_path) as conn:
        conn.execute(
            f"""
            UPDATE A64Entry
            SET {col}=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE pk_ID IN (
              SELECT e.pk_ID FROM A64Entry e
              JOIN A64Result r ON r.pk_ID=e.fk_A64Result_ID
              WHERE r.a64_id=? AND r.a64_category=? AND e.entry_index=?
            )
            """,
            (str(row.get("a64_id")), int(row.get("a64_category") or 0), int(row.get("entry_index") or 0)),
        )
    row["status"] = state
    row["a64_status"] = state
    return True


def clear_a64_entry_state(db_path, row: dict[str, Any]) -> bool:
    if not is_a64_row(row):
        return False
    with a64_db_connect(db_path) as conn:
        conn.execute(
            """
            UPDATE A64Entry
            SET failed_at=NULL, discarded_at=NULL, deleted_at=NULL, tested_at=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE pk_ID IN (
              SELECT e.pk_ID FROM A64Entry e
              JOIN A64Result r ON r.pk_ID=e.fk_A64Result_ID
              WHERE r.a64_id=? AND r.a64_category=? AND e.entry_index=?
            )
            """,
            (str(row.get("a64_id")), int(row.get("a64_category") or 0), int(row.get("entry_index") or 0)),
        )
    row["status"] = "downloaded" if row.get("downloaded_at") else "seen"
    row["a64_status"] = row["status"]
    return True


def a64_record_local_candidate(db_path, local_path, title, source_row: dict[str, Any], file_type=None) -> None:
    p = Path(local_path)
    ext = file_type or p.suffix.lower().lstrip(".") or "other"
    source_id = str(source_row.get("a64_id") or "local")
    result = {
        "id": f"{source_id}-local-{p.stem}",
        "category": int(source_row.get("a64_category") or 0),
        "name": title,
        "group": source_row.get("group_name", ""),
        "year": int(source_row.get("year") or 0),
    }
    entry = type(
        "Entry",
        (),
        {
            "entry_index": 0,
            "path": p.name,
            "suffix": ext,
            "size": p.stat().st_size if p.exists() else None,
            "date": None,
            "raw": {"path": p.name, "id": 0, "size": p.stat().st_size if p.exists() else None, "derived_from": source_id},
        },
    )()
    with a64_db_connect(db_path) as conn:
        result_pk = a64_upsert_result(conn, result)
        entry_pk = a64_upsert_entry(conn, result_pk, entry, str(p))
        conn.execute(
            "UPDATE A64Entry SET title=?, deleted_at=NULL, discarded_at=NULL, failed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE pk_ID=?",
            (title, entry_pk),
        )


def a64_inbox_files(root=DEFAULT_INBOX) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_file())


def reconcile_a64_inbox(state_path, root=DEFAULT_INBOX) -> list[Path]:
    files = a64_inbox_files(root)
    pat = re.compile(r"^(.+?)_(\d+)_(\d+)_(.+)$")
    with a64_db_connect(state_path) as conn:
        for p in files:
            conn.execute(
                "UPDATE A64Entry SET deleted_at=NULL, discarded_at=NULL, failed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE local_path=?",
                (str(p),),
            )
            if ".converted." in p.name:
                continue
            m = pat.match(p.name)
            if not m:
                continue
            rid, cat, idx, original = m.groups()
            result = {"id": rid, "category": int(cat), "name": Path(original).stem}
            result_pk = a64_upsert_result(conn, result)
            entry = type(
                "Entry",
                (),
                {
                    "entry_index": int(idx),
                    "path": original,
                    "suffix": Path(original).suffix.lower().lstrip(".") or "other",
                    "size": p.stat().st_size,
                    "date": None,
                    "raw": {"path": original, "id": int(idx), "size": p.stat().st_size},
                },
            )()
            a64_upsert_entry(conn, result_pk, entry, str(p))
    return files


def clear_a64_inbox(state_path, root=DEFAULT_INBOX) -> int:
    files = a64_inbox_files(root)
    paths = [str(p) for p in files]
    for p in files:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    if paths:
        with a64_db_connect(state_path) as conn:
            conn.executemany(
                "UPDATE A64Entry SET deleted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE local_path=?",
                [(p,) for p in paths],
            )
    return len(paths)


def promote_a64_candidate(host, db_path, row: dict[str, Any], title: str, destination_path: str) -> tuple[str, dict[str, Any]]:
    if not is_local_a64_row(row):
        raise RuntimeError("Selected row is not a local A64 candidate")
    local = Path(row.get("path", ""))
    if not local.exists():
        raise RuntimeError(f"Local file missing: {local}")
    dest = str(destination_path or "").strip()
    if not dest:
        raise RuntimeError("Promotion cancelled: no destination")
    if not dest.startswith("/"):
        dest = "/Usb0/" + dest.lstrip("/")

    uploaded = ftp_upload(host, dest, local.read_bytes())
    image_path = strip_usb_prefix(uploaded)
    ext = local.suffix.lower().lstrip(".")
    mode = mode_for_ext(ext)
    notes = f"A64 id={row.get('a64_id')} category={row.get('a64_category')} entry={row.get('entry_index')} original={row.get('original_filename') or local.name}"
    payload = payload_for(mode, image_path, "")
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        vals = {
            "title": title,
            "path": image_path,
            "payload": payload,
            "entry": "",
            "detail": f"A64 promoted {ext.upper()}",
            "notes": notes,
            "quarantine_reason": "",
            "deleted_reason": "",
            "quarantined": 0,
            "fk_Status_ID": lookup_id(conn, "Status", "approved"),
            "fk_StorageStatus_ID": lookup_id(conn, "StorageStatus", "present"),
            "fk_MachineMode_ID": lookup_id(conn, "MachineMode", row.get("machine_mode") or "c64"),
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
    with a64_db_connect(db_path) as conn:
        conn.execute(
            """
            UPDATE A64Entry
            SET promoted_path=?, promoted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE pk_ID IN (
              SELECT e.pk_ID FROM A64Entry e JOIN A64Result r ON r.pk_ID=e.fk_A64Result_ID
              WHERE r.a64_id=? AND r.a64_category=? AND e.entry_index=?
            )
            """,
            (image_path, str(row.get("a64_id")), int(row.get("a64_category") or 0), int(row.get("entry_index") or 0)),
        )
    try:
        local.unlink()
    except FileNotFoundError:
        pass
    new_row = {
        "title": title,
        "path": image_path,
        "payload": payload,
        "mode": mode,
        "entry": "",
        "machine_mode": row.get("machine_mode") or "c64",
        "file_type": ext,
        "type": ext,
        "detail": f"A64 promoted {ext.upper()}",
        "status": "approved",
        "storage_status": "present",
        "notes": notes,
    }
    row["promoted_path"] = image_path
    row["promoted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    row["status"] = "promoted"
    row["a64_status"] = "promoted"
    return image_path, new_row
