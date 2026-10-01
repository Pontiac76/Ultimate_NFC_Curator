#!/usr/bin/env python3
"""Assembly64 client helpers.

Implements the public subset used by Ultimate firmware:

    GET /leet/search/aql/presets
    GET /leet/search/aql?query=<AQL>
    GET /leet/search/entries/<id>/<category>
    GET /leet/search/bin/<id>/<category>/<entry-index>

This module intentionally does not mutate downloaded content. A64 intake is a
local candidate workflow; promotion/testing belongs to higher-level tools.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from u2_common import sqlite_connect
except Exception:  # pragma: no cover - keeps standalone probing usable
    sqlite_connect = None

DEFAULT_BASE_URL = "http://hackerswithstyle.se"
DEFAULT_TIMEOUT = 30
DEFAULT_HEADERS = {
    "Accept-encoding": "identity",
    "User-Agent": "Assembly Query",
    "Client-Id": "Ultimate",
}

TYPE_DISPLAY_ORDER = ["prg", "d64", "d71", "d81", "crt", "tap", "g64", "t64", "sid", "txt", "other"]
LAUNCHABLE_TYPES = {"prg", "d64", "d71", "d81", "crt", "sid"}
REFERENCE_ONLY_TYPES = {"tap", "t64", "g64"}
DEFAULT_INBOX = Path("a64_inbox")


@dataclass(frozen=True)
class A64Entry:
    result_id: str
    category: int
    entry_index: int
    path: str
    size: int | None = None
    date: int | None = None
    raw: dict[str, Any] | None = None

    @property
    def suffix(self) -> str:
        p = Path(self.path)
        return p.suffix.lower().lstrip(".") or "other"


class Assembly64Client:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, path: str) -> tuple[urllib.response.addinfourl, bytes]:
        req = urllib.request.Request(self.base_url + path, headers=DEFAULT_HEADERS)
        resp = urllib.request.urlopen(req, timeout=self.timeout)
        return resp, resp.read()

    def _json(self, path: str) -> Any:
        resp, body = self._request(path)
        text = body.decode("utf-8", "replace")
        return json.loads(text)

    def presets(self) -> list[dict[str, Any]]:
        return self._json("/leet/search/aql/presets")

    def search_aql(self, query: str, start: int | None = None, count: int | None = None) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(query)
        page = ""
        if start is not None or count is not None:
            page = f"/{int(start or 0)}/{int(count or 100)}"
        return self._json(f"/leet/search/aql{page}?query={encoded}")

    def entries(self, result_id: str, category: int) -> list[A64Entry]:
        encoded_id = urllib.parse.quote(str(result_id))
        obj = self._json(f"/leet/search/entries/{encoded_id}/{int(category)}")
        out: list[A64Entry] = []
        for item in obj.get("contentEntry", []) if isinstance(obj, dict) else []:
            idx = int(item.get("id", len(out)))
            out.append(
                A64Entry(
                    result_id=str(result_id),
                    category=int(category),
                    entry_index=idx,
                    path=str(item.get("path", "")),
                    size=item.get("size"),
                    date=item.get("date"),
                    raw=item,
                )
            )
        return out

    def download_entry(self, result_id: str, category: int, entry_index: int, out_path: str | Path) -> Path:
        encoded_id = urllib.parse.quote(str(result_id))
        path = f"/leet/search/bin/{encoded_id}/{int(category)}/{int(entry_index)}"
        resp, body = self._request(path)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        return out


def aql_term(key: str, value: str) -> str:
    value = str(value).strip().lower()
    if not value:
        return ""
    if re.search(r"\s|[():&|]", value):
        value = value.replace('"', r'\"')
        return f'({key}:"{value}")'
    return f"({key}:{value})"


def aql_or_terms(key: str, values) -> str:
    clean = [str(v).strip().lower() for v in values if str(v).strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return aql_term(key, clean[0])
    return "(" + " | ".join(aql_term(key, v) for v in clean) + ")"


def split_aql_values(value: str):
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def build_aql(
    *,
    name: str = "",
    group: str = "",
    handle: str = "",
    repo: str = "",
    category: str = "",
    subcat: str = "",
    types: Iterable[str] = (),
    sort: str = "",
    order: str = "",
    latest: str = "",
    rating: str = "",
) -> str:
    terms = []
    for key, value in (
        ("name", name),
        ("group", group),
        ("handle", handle),
        ("sort", sort),
        ("order", order),
        ("latest", latest),
        ("rating", rating),
    ):
        t = aql_term(key, value)
        if t:
            terms.append(t)
    for key, value in (("repo", repo), ("category", category), ("subcat", subcat)):
        t = aql_or_terms(key, split_aql_values(value))
        if t:
            terms.append(t)
    clean_types = [t.lower().lstrip(".") for t in types if str(t).strip()]
    if len(clean_types) == 1:
        terms.append(aql_term("type", clean_types[0]))
    elif len(clean_types) > 1:
        terms.append("(" + " | ".join(aql_term("type", t) for t in clean_types) + ")")
    return " & ".join(terms) if terms else ""


def search_and_optionally_record(client: Assembly64Client, query: str, db_path: str | None = None, start: int | None = None, count: int | None = None) -> list[dict[str, Any]]:
    results = client.search_aql(query, start, count)
    if db_path:
        with db_connect(db_path) as conn:
            for r in results:
                upsert_result(conn, r)
    return results


def entry_type_summary(entries: Iterable[A64Entry]) -> str:
    seen = {e.suffix for e in entries}
    def key(t: str) -> int:
        return TYPE_DISPLAY_ORDER.index(t) if t in TYPE_DISPLAY_ORDER else len(TYPE_DISPLAY_ORDER)
    return ",".join(sorted(seen, key=key))


def file_set_label(entries: list[A64Entry]) -> str:
    launchable = [e for e in entries if e.suffix in LAUNCHABLE_TYPES]
    disk_count = sum(1 for e in launchable if e.suffix in {"d64", "d71", "d81"})
    if disk_count > 1 and len({e.suffix for e in launchable if e.suffix in {"d64", "d71", "d81"}}) == 1:
        return f"{disk_count} disk set"
    if len(launchable) == 1:
        return "1 file"
    return f"{len(launchable) or len(entries)} files"


def safe_inbox_filename(result_id: str, category: int, entry: A64Entry) -> str:
    original = Path(entry.path).name or f"entry{entry.entry_index}.bin"
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", original).strip(" .") or "download.bin"
    return f"{result_id}_{int(category)}_{entry.entry_index}_{safe}"


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
    result_cols = {r[1] for r in conn.execute("PRAGMA table_info(A64Result)")}
    for col, spec in (
        ("entries_checked_at", "TEXT"),
        ("entries_http_status", "INTEGER"),
        ("entries_count", "INTEGER"),
        ("entries_empty_at", "TEXT"),
    ):
        if col not in result_cols:
            conn.execute(f"ALTER TABLE A64Result ADD COLUMN {col} {spec}")
    existing = {r[1] for r in conn.execute("PRAGMA table_info(A64Entry)")}
    for col in ("failed_at", "deleted_at", "tested_at"):
        if col not in existing:
            conn.execute(f"ALTER TABLE A64Entry ADD COLUMN {col} TEXT")
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
            a64_id, cat, name, name, result.get("group", ""), result.get("handle", ""), int(result.get("year") or 0),
            result.get("released", ""), result.get("updated", ""), int(result.get("rating") or 0),
            int(result.get("siteCategory") or 0), float(result.get("siteRating") or 0), json.dumps(result, ensure_ascii=False),
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
        (result_pk, entry.entry_index, Path(original).stem, original, entry.suffix, entry.size, entry.date, local_path, local_path, json.dumps(entry.raw or {}, ensure_ascii=False)),
    )
    return conn.execute("SELECT pk_ID FROM A64Entry WHERE fk_A64Result_ID=? AND entry_index=?", (result_pk, entry.entry_index)).fetchone()[0]


def record_download(db_path: str | Path, result: dict[str, Any], entry: A64Entry, local_path: str | Path) -> None:
    with db_connect(db_path) as conn:
        result_pk = upsert_result(conn, result)
        upsert_entry(conn, result_pk, entry, str(local_path))


def _cmd_presets(args) -> int:
    client = Assembly64Client(args.base_url, args.timeout)
    print(json.dumps(client.presets(), indent=2, ensure_ascii=False))
    return 0


def _cmd_search(args) -> int:
    client = Assembly64Client(args.base_url, args.timeout)
    query = args.query or build_aql(
        name=args.name,
        group=args.group,
        handle=args.handle,
        repo=args.repo,
        category=args.category,
        subcat=args.subcat,
        types=args.type or [],
        sort=args.sort,
        order=args.order,
        latest=args.latest,
        rating=args.rating,
    )
    if not query:
        raise SystemExit("No query supplied")
    results = search_and_optionally_record(client, query, args.db, args.start, args.count)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(f"AQL: {query}")
        for i, r in enumerate(results, 1):
            name = r.get("name", "")
            group = r.get("group", "")
            year = r.get("year", "") or ""
            rid = r.get("id", "")
            cat = r.get("category", "")
            print(f"{i:3}. {name} | {group} | {year} | id={rid} cat={cat}")
    return 0


def _cmd_entries(args) -> int:
    client = Assembly64Client(args.base_url, args.timeout)
    entries = client.entries(args.id, args.category)
    if args.json:
        print(json.dumps([e.raw for e in entries], indent=2, ensure_ascii=False))
    else:
        print(f"{file_set_label(entries)} | type(s): {entry_type_summary(entries)}")
        for e in entries:
            print(f"{e.entry_index:3}. {e.path} | {e.suffix} | {e.size or ''}")
    return 0


def _cmd_download(args) -> int:
    client = Assembly64Client(args.base_url, args.timeout)
    entries = client.entries(args.id, args.category)
    selected = entries if args.all else [e for e in entries if e.entry_index == args.index]
    if not selected:
        raise SystemExit("No matching entry")
    out_dir = Path(args.out_dir)
    result = {"id": str(args.id), "category": int(args.category), "name": args.title or str(args.id)}
    if args.db and not args.title:
        # Preserve richer result metadata if this ID was just found by search.
        with db_connect(args.db) as conn:
            row = conn.execute("SELECT raw_json FROM A64Result WHERE a64_id=? AND a64_category=?", (str(args.id), int(args.category))).fetchone()
            if row and row[0]:
                try:
                    result = json.loads(row[0])
                except Exception:
                    pass
    for e in selected:
        out = out_dir / safe_inbox_filename(args.id, args.category, e)
        client.download_entry(args.id, args.category, e.entry_index, out)
        if args.db:
            record_download(args.db, result, e, out)
        print(out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assembly64 client")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("presets").set_defaults(func=_cmd_presets)

    s = sub.add_parser("search")
    s.add_argument("query", nargs="?", help='Raw AQL, e.g. (name:"frogger") & (type:d64)')
    s.add_argument("--name", default="")
    s.add_argument("--group", default="")
    s.add_argument("--handle", default="")
    s.add_argument("--repo", default="")
    s.add_argument("--category", default="")
    s.add_argument("--subcat", default="")
    s.add_argument("--type", action="append", default=[])
    s.add_argument("--sort", default="")
    s.add_argument("--order", default="")
    s.add_argument("--latest", default="")
    s.add_argument("--rating", default="")
    s.add_argument("--start", type=int)
    s.add_argument("--count", type=int)
    s.add_argument("--json", action="store_true")
    s.add_argument("--db", help="record returned results in curator/A64 DB")
    s.set_defaults(func=_cmd_search)

    e = sub.add_parser("entries")
    e.add_argument("id")
    e.add_argument("category", type=int)
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=_cmd_entries)

    d = sub.add_parser("download")
    d.add_argument("id")
    d.add_argument("category", type=int)
    d.add_argument("index", type=int, nargs="?", default=0)
    d.add_argument("--all", action="store_true")
    d.add_argument("--out-dir", default=str(DEFAULT_INBOX))
    d.add_argument("--db", help="record downloaded entries in curator/A64 DB")
    d.add_argument("--title", default="", help="fallback editable title if result metadata is not already in DB")
    d.set_defaults(func=_cmd_download)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
