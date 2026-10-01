# Assembly64 API Notes

Discovery source:

```text
https://raw.githubusercontent.com/GideonZ/1541ultimate/refs/heads/master/software/network/assembly.cc
```

The Ultimate firmware talks to Assembly64 through `hackerswithstyle.se` over plain HTTP.

## Host

```text
host: hackerswithstyle.se
port: 80
```

## Endpoints seen in Ultimate source

```text
/leet/search/aql?query=
/leet/search/aql/presets
/leet/search/entries
/leet/search/bin
```

Source constants:

```c
#define HOSTNAME      "hackerswithstyle.se"
#define HOSTPORT      80
#define URL_SEARCH    "/leet/search/aql?query="
#define URL_PATTERNS  "/leet/search/aql/presets"
#define URL_ENTRIES   "/leet/search/entries"
#define URL_DOWNLOAD  "/leet/search/bin"
```

## Query fields from presets

Live `/leet/search/aql/presets` reports these filter fields:

```text
repo      csdb, gamebase, oneload, utape, c64com, tapes, guybrush, hvsc, c64orgintro
category  demos, games, intros, c128, bbs, charts, mags, easyflash, graphics, misc, music, tools
subcat    c64comdemos, c64comgames, intros, c128stuff, bbs, charts, demos, discmags,
          easyflash, games, graphics, c64misc, music, tools, gamebase, guybrushdemos,
          guybrushgames, guybrushgamesgerman, guybrushmisc, guybrushutils,
          guybrushutilsgerman, hvscdemos, hvscgames, hvscmusic, oneload64,
          presdisk, prestap
rating    >=1 .. >=10
type      bin, crt, d64, d71, d81, g64, prg, sid, t64, tap
date      1980 .. current year
latest    1days, 2days, 4days, 1week, 2weeks, 3weeks, 1month, 2months,
          3months, 6months, 1year, 2years
sort      name, group, handle, event, year, rating
order     asc, desc
```

For curator integration, the likely useful first-pass fields are:

```text
name
type
repo
category/subcat
sort/order
```

`date`, `event`, `handle`, and `rating` can be left for later unless a real workflow needs them.

## Confirmed flow

1. Query presets from:
   ```text
   GET /leet/search/aql/presets
   ```
2. Submit text/AQL search to:
   ```text
   GET /leet/search/aql?query=<url-encoded-query>
   ```
3. Retrieve matching entry metadata from:
   ```text
   GET /leet/search/entries/<id>/<category>
   ```
4. Download binary from:
   ```text
   GET /leet/search/bin/<id>/<category>/<entry-index>
   ```

## Implementation direction

Initial client module:

```text
u2_assembly64.py
```

Useful commands:

```bash
./u2_assembly64.py presets
./u2_assembly64.py search --name frogger --category games --type d64 --type prg --count 20
./u2_assembly64.py entries 255780 0
./u2_assembly64.py download 255780 0 --out-dir a64_inbox
```

Raw AQL is also supported:

```bash
./u2_assembly64.py search '(name:"frogger") & (category:games) & ((type:d64) | (type:prg))'
```

Main module pieces:

```python
Assembly64Client.presets()
Assembly64Client.search_aql(query, start=None, count=None)
Assembly64Client.entries(result_id, category)
Assembly64Client.download_entry(result_id, category, entry_index, out_path)
build_aql(...)
entry_type_summary(entries)
file_set_label(entries)
```

## Curator integration idea

Future TUI flow:

```text
F10 -> Search A64...
```

A64 downloads are local intake candidates, not immediate permanent U2 library files.

Lifecycle:

```text
search -> select/download -> local inbox -> optional temp upload/test -> promote unchanged OR discard
```

Rules:

- Do not modify downloaded file contents during A64 intake.
- Do not rewrite internal disk titles/IDs.
- Conversion/merge workflows are separate explicit actions.
- Downloaded candidates should have editable curator-facing titles in the A64 intake table.
- Titles do not need to be unique.
- Permanent promoted filenames should retain an A64 identifier to avoid collisions.
- Promoted path convention should keep titles browseable, not bury files in ID folders.

Suggested promoted path convention:

```text
/A64/{bucket}/{Title}-{a64_id}.{ext}
```

Examples:

```text
/A64/F/Frogger-255780.d64
/A64/F/Frogger-c85d7251b91ada7601d61a6dd5c2de4b.prg
/A64/E/Eye of the Beholder-123456-disk1.d64
/A64/E/Eye of the Beholder-123456-disk2.d64
```

`bucket` is `A`-`Z` or `#`.

For multi-entry results, promote selected entries together into the same title folder/bucket pattern if needed, while preserving unique IDs in filenames.

Result display should summarize file sets with unique types only:

```text
Eye of the Beholder | group | year | 2 disk set | type(s): d64
Some Title | group | year | 3 files | type(s): prg,d64,txt
```

Type order for display:

```text
prg, d64, d71, d81, crt, tap, g64, t64, sid, txt, other
```

A64 candidate testing can upload to a temporary U2 path such as `/Temp/A64/` if available, otherwise a managed scratch directory such as `/Usb0/_A64_Test/`.

## Background/throttled downloads

A64 downloads should be able to run in the background so the TUI stays usable.

Desired behavior:

- User selects one/many results or entries.
- TUI queues downloads.
- A background worker downloads them slowly/intentionally politely.
- Randomized waits are inserted between downloads.
- DB is updated when each file finishes.
- TUI polls local DB/status once per second and shows A64 activity below drive status, e.g.:

```text
A64: Downloading Frogger-255780.d64
A64: Waiting 17s before next download; last grabbed BeachHead-249819.d64
A64: Queue idle; 12 downloaded, 1 failed
```

Implementation options:

- Prefer a small Python worker for portability and direct DB updates.
- `wget` could be used, but a Python worker avoids parsing external output and can update status rows atomically.
- Worker should write status to DB and/or a small status JSON file.

Potential tables:

```text
A64DownloadQueue
A64DownloadStatus
```

Queue row states:

```text
queued, downloading, waiting, complete, failed, cancelled
```

Downloaded candidates should remain local until promoted or discarded.
