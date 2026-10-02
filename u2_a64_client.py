#!/usr/bin/env python3
"""Assembly64 HTTP client and AQL helpers.

This module intentionally has no SQLite or TUI responsibilities. It knows how
to talk to Assembly64 and how to summarize entry metadata; callers decide what
to persist or display. Astonishing restraint, given the previous file's hobbies.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

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
        _resp, body = self._request(path)
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
        _resp, body = self._request(path)
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
