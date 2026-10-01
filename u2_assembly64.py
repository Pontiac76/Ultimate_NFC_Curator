#!/usr/bin/env python3
"""Assembly64 command-line interface.

The HTTP/AQL client lives in `u2_a64_client.py`; SQLite persistence lives in
`u2_a64_db.py`. This file stays as the compatibility CLI entry point.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from u2_a64_client import (
    DEFAULT_BASE_URL,
    DEFAULT_INBOX,
    DEFAULT_TIMEOUT,
    LAUNCHABLE_TYPES,
    REFERENCE_ONLY_TYPES,
    A64Entry,
    Assembly64Client,
    build_aql,
    entry_type_summary,
    file_set_label,
    safe_inbox_filename,
)
from u2_a64_db import (
    db_connect,
    ensure_a64_tables,
    record_download,
    record_entries_check,
    search_and_optionally_record,
    upsert_entry,
    upsert_result,
)

# Backwards-compatible aliases for existing imports during the refactor.
a64_db_connect = db_connect


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
