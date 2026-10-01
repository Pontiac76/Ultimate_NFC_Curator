#!/usr/bin/env python3
"""Assembly64 SQLite persistence helpers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from u2_a64_client import A64Entry, Assembly64Client

try:
    from u2_common import sqlite_connect
except Exception:  # pragma: no cover - keeps standalone probing usable
    sqlite_connect = None


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, spec in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def ensure_a64_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS A64Result (
            pk_ID INTEGER PRIMARY KEY AUTOINCREMENT,
            a64_id TEXT NOT NULL,
            a64_category INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            a64_name TEXT NOT NULL DEFAULT '',
            group_name TEXT NOT NULL DEFAULT '',
            handle TEXT NOT NULL DEFAULT '',
            year INTEGER NOT NULL DEFAULT 0,
            released TEXT NOT NULL DEFAULT '',
            updated TEXT NOT NULL DEFAULT '',
            rating INTEGER NOT NULL DEFAULT 0,
            site_category INTEGER NOT NULL DEFAULT 0,
            site_rating REAL NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(a64_id, a64_category)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS A64Entry (
            pk_ID INTEGER PRIMARY KEY AUTOINCREMENT,
            fk_A64Result_ID INTEGER NOT NULL REFERENCES A64Result(pk_ID) ON DELETE CASCADE,
            entry_index INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            original_filename TEXT NOT NULL DEFAULT '',
            file_type TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER,
            a64_date INTEGER,
            local_path TEXT NOT NULL DEFAULT '',
            downloaded_at TEXT,
            discarded_at TEXT,
            promoted_path TEXT NOT NULL DEFAULT '',
            promoted_at TEXT,
            raw_json TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(fk_A64Result_ID, entry_index)
        )
        """
    )
    _ensure_columns(
        conn,
        "A64Result",
        (
            ("entries_checked_at", "TEXT"),
            ("entries_http_status", "INTEGER"),
            ("entries_count", "INTEGER"),
            ("entries_empty_at", "TEXT"),
        ),
    )
    _ensure_columns(
        conn,
        "A64Entry",
        (
            ("discarded_at", "TEXT"),
            ("promoted_path", "TEXT NOT NULL DEFAULT ''"),
            ("promoted_at", "TEXT"),
            ("failed_at", "TEXT"),
            ("deleted_at", "TEXT"),
            ("tested_at", "TEXT"),
        ),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_A64Entry_local_path ON A64Entry(local_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_A64Entry_promoted_path ON A64Entry(promoted_path)")
    conn.execute("DROP VIEW IF EXISTS a64_download_rows")
    conn.execute(
        """
        CREATE VIEW a64_download_rows AS
        SELECT
            e.pk_ID AS id,
            r.a64_id,
            r.a64_category,
            e.entry_index,
            COALESCE(NULLIF(r.title, ''), NULLIF(e.title, ''), r.a64_name, e.original_filename) AS title,
            r.title AS result_title,
            r.a64_name,
            r.group_name,
            r.handle,
            r.year,
            r.entries_count,
            r.entries_checked_at,
            e.original_filename,
            e.file_type,
            e.size_bytes,
            e.local_path,
            e.downloaded_at,
            e.discarded_at,
            e.promoted_path,
            e.promoted_at,
            e.failed_at,
            e.deleted_at,
            e.tested_at,
            CASE
                WHEN e.deleted_at IS NOT NULL THEN 'deleted'
                WHEN e.promoted_at IS NOT NULL THEN 'promoted'
                WHEN e.failed_at IS NOT NULL THEN 'failed'
                WHEN e.discarded_at IS NOT NULL THEN 'discarded'
                WHEN e.tested_at IS NOT NULL THEN 'tested'
                WHEN e.downloaded_at IS NOT NULL THEN 'downloaded'
                ELSE 'seen'
            END AS a64_status
        FROM A64Entry e
        JOIN A64Result r ON r.pk_ID = e.fk_A64Result_ID
        """
    )


def db_connect(path: str | Path) -> sqlite3.Connection:
    if sqlite_connect is not None:
        conn = sqlite_connect(path)
    else:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
    ensure_a64_tables(conn)
    return conn


def upsert_result(conn: sqlite3.Connection, result: dict[str, Any]) -> int:
    a64_id = str(result.get("id", ""))
    cat = int(result.get("category", 0))
    name = str(result.get("name", ""))
    conn.execute(
        """
        INSERT INTO A64Result (
            a64_id, a64_category, title, a64_name, group_name, handle, year,
            released, updated, rating, site_category, site_rating, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(a64_id, a64_category) DO UPDATE SET
            a64_name=excluded.a64_name,
            group_name=excluded.group_name,
            handle=excluded.handle,
            year=excluded.year,
            released=excluded.released,
            updated=excluded.updated,
            rating=excluded.rating,
            site_category=excluded.site_category,
            site_rating=excluded.site_rating,
            raw_json=excluded.raw_json,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            a64_id,
            cat,
            name,
            name,
            result.get("group", ""),
            result.get("handle", ""),
            int(result.get("year") or 0),
            result.get("released", ""),
            result.get("updated", ""),
            int(result.get("rating") or 0),
            int(result.get("siteCategory") or 0),
            float(result.get("siteRating") or 0),
            json.dumps(result, ensure_ascii=False),
        ),
    )
    return conn.execute("SELECT pk_ID FROM A64Result WHERE a64_id=? AND a64_category=?", (a64_id, cat)).fetchone()[0]


def record_entries_check(conn: sqlite3.Connection, result_pk: int, http_status: int, entry_count: int) -> None:
    conn.execute(
        """
        UPDATE A64Result
        SET entries_checked_at=CURRENT_TIMESTAMP,
            entries_http_status=?,
            entries_count=?,
            entries_empty_at=CASE WHEN ?=0 THEN CURRENT_TIMESTAMP ELSE entries_empty_at END,
            updated_at=CURRENT_TIMESTAMP
        WHERE pk_ID=?
        """,
        (int(http_status), int(entry_count), int(entry_count), int(result_pk)),
    )


def upsert_entry(conn: sqlite3.Connection, result_pk: int, entry: A64Entry, local_path: str = "") -> int:
    original = Path(entry.path).name
    conn.execute(
        """
        INSERT INTO A64Entry (
            fk_A64Result_ID, entry_index, title, original_filename, file_type,
            size_bytes, a64_date, local_path, downloaded_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? != '' THEN CURRENT_TIMESTAMP ELSE NULL END, ?)
        ON CONFLICT(fk_A64Result_ID, entry_index) DO UPDATE SET
            original_filename=excluded.original_filename,
            file_type=excluded.file_type,
            size_bytes=excluded.size_bytes,
            a64_date=excluded.a64_date,
            local_path=CASE WHEN excluded.local_path != '' THEN excluded.local_path ELSE A64Entry.local_path END,
            downloaded_at=CASE WHEN excluded.local_path != '' THEN CURRENT_TIMESTAMP ELSE A64Entry.downloaded_at END,
            deleted_at=CASE WHEN excluded.local_path != '' THEN NULL ELSE A64Entry.deleted_at END,
            discarded_at=CASE WHEN excluded.local_path != '' THEN NULL ELSE A64Entry.discarded_at END,
            failed_at=CASE WHEN excluded.local_path != '' THEN NULL ELSE A64Entry.failed_at END,
            raw_json=excluded.raw_json,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            result_pk,
            entry.entry_index,
            Path(original).stem,
            original,
            entry.suffix,
            entry.size,
            entry.date,
            local_path,
            local_path,
            json.dumps(entry.raw or {}, ensure_ascii=False),
        ),
    )
    return conn.execute("SELECT pk_ID FROM A64Entry WHERE fk_A64Result_ID=? AND entry_index=?", (result_pk, entry.entry_index)).fetchone()[0]


def record_download(db_path: str | Path, result: dict[str, Any], entry: A64Entry, local_path: str | Path) -> None:
    with db_connect(db_path) as conn:
        result_pk = upsert_result(conn, result)
        upsert_entry(conn, result_pk, entry, str(local_path))


def search_and_optionally_record(
    client: Assembly64Client,
    query: str,
    db_path: str | None = None,
    start: int | None = None,
    count: int | None = None,
) -> list[dict[str, Any]]:
    results = client.search_aql(query, start, count)
    if db_path:
        with db_connect(db_path) as conn:
            for r in results:
                upsert_result(conn, r)
    return results
