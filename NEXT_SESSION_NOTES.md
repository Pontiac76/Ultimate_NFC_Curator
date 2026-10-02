# Next Session Notes

## Current branch/context

Current branch: `assembly64-integration`.

This branch is now broader than public A64 search only. It includes:

- A64 public API client/search/download workflow.
- Local A64 inbox/cache workflow.
- A64 candidate testing/promotion/deletion.
- A64 F10 image actions/conversion work in progress.
- Local disk-speed/load benchmark scratch work under ignored `test/`.
- Discovery that a large Assembly64-style archive exists locally on Windows and can be searched through Everything HTTP.

## Important A64 curator behavior/decisions

A64 downloads are local candidates first, not immediate U2 library files.

Lifecycle:

```text
search -> download to a64_inbox -> test -> promote unchanged OR delete/discard
```

Rules:

- Do not mutate original A64 downloads during intake.
- Conversion creates a new candidate in `a64_inbox/`.
- Promotion uploads unchanged selected candidate to permanent `/A64/...` path and inserts/updates normal `Image` row.
- Promoted normal `Image` rows are marked `approved` because promotion implies tested/accepted.
- After successful promotion, local cached source file should be deleted from `a64_inbox/` so promotion behaves like a move.
- A64 inbox should not prompt to resume immediately after downloading; it should enter inbox directly.
- Pressing `q` in A64 inbox returns to curator, not exits app.
- Pressing `y` while already in A64 inbox should not recurse; it should say already in inbox.
- `x` works in both normal curator mode and A64 mode, with confirmation.
- A64 delete removes local candidate, updates A64 DB state, removes row from visible list; if promoted path exists, it attempts to delete U2 copy too.
- Normal curator delete removes U2 file, marks `Image.storage_status = deleted`, sets `deleted_reason`, removes visible row.

## GitHub issue tracker

Bug reports and future enhancement notes have been moved to GitHub issues.

Current project issues:

```bash
gh issue list
```

## A64 DB/state details

A64 tables/views currently include:

- `A64Result`
- `A64Entry`
- `a64_download_rows`

A64 entry status is computed from timestamps:

```text
deleted -> promoted -> failed -> discarded -> tested -> downloaded -> seen
```

If a file exists in `a64_inbox/`, reconcile should clear stale:

```text
deleted_at
discarded_at
failed_at
```

because recreated/reconverted/redownloaded files are available again.

A64 inbox sorting uses production constant:

```python
A64_INBOX_ORDER_BY
```

and test imports that source constant:

```text
tests/test_a64_sort.py
```

Testing practice going forward:

- Production source owns query fragments/rules/constants.
- Tests import production symbols and validate expected behavior with controlled data.
- Avoid duplicating source logic in tests.

## A64 F10/conversion status

The desired direction is shared UI/action behavior, not separate ad-hoc A64 screens.

Current state:

- `choose_menu()` was restored to centered popup menu aesthetic.
- F10 works in both normal curator rows and A64 rows.
- A64 F10 uses the same popup style and shared action item generation.
- A64 conversion targets should mirror main progression:

```text
PRG -> D64/D71/D81
D64 -> D71/D81
D71 -> D81
```

Converted candidates:

- stay in `a64_inbox/`
- are recorded as A64 local candidates
- should preserve/derive title from source row, e.g.:
  `Beach Head - Converted [to D71]`
- inbox refreshes after conversion

Known caveat: main branch did not have image-conversion F10 merged; image-conversion branch contains richer prior work. Need to reconcile/reuse code rather than reinventing.

## NFC/reader notes

WSL CH340/PN532 reader can wedge with `/dev/ttyUSB0` present but I/O failing:

```text
OSError: [Errno 5] Input/output error: /dev/ttyUSB0
ch341-uart ttyUSB0: failed to read modem status: -32
```

Fix tested:

```text
usbipd detach --busid <BUSID>
sleep 2
usbipd attach --wsl --busid <BUSID>
```

`scripts/attach-nfc-wsl.sh` now force-detaches/reattaches if device is already attached to clear stale CH340/WSL I/O state.

Curator startup checks NFC reader and shows readiness/unavailable status in TUI footer. NFC write retry attempts reattach.

## PRG sidecar D81 behavior

For `.prg` DMA launches, tooling creates/mounts sidecar D81:

```text
<program>.prg.d81
```

Purpose: if a DMA-loaded PRG wants drive 8 for saves/loads, it gets a matching writable disk.

Inventory skips:

```text
*.prg.d81
```

A64 local PRG testing does the same via `/Usb0/_A64_Test/<filename>.prg.d81`.

## Disk speed/load testing conclusions

Scratch scripts are under ignored `test/`:

- `test/basic_tokenizer.py`
- `test/disk_speed_test.py`
- `test/load_speed_test.py`

U2 BASIC editor discovery:

- Web UI tokenizes BASIC in JS.
- Writes tokenized BASIC via:

```text
POST /v1/machine:writemem?address=0801
PUT  /v1/machine:writemem?address=002d&data=<little-endian-varptr>
```

A Python BASIC V2 tokenizer was written in `test/basic_tokenizer.py` for test injection.

Throughput/testing conclusion so far:

- BASIC `GET#` benchmark is heavily BASIC-throttled.
- KERNAL `LOAD"*",8` benchmark for ~10K PRG showed essentially no meaningful difference:

```text
1541/D64: 1048 jiffies / 000017
1571/D71: 1065 jiffies / 000017
1581/D81: 1038 jiffies / 000017
```

Interpretation:

- D71/D81 up-conversion is still useful for consolidation/fewer disk swaps/show workflow.
- But raw C64-mode KERNAL load speed is not meaningfully improved merely by D64 vs D71 vs D81 image type.
- User still may up-convert because some real games/loaders feel different; tests only cover these specific workloads.

## Local Everything / Assembly64 archive discovery

Windows Everything HTTP server temporarily available at:

```text
http://192.168.4.2:3050
```

Found local Assembly64/archive content:

```text
C:\Users\Stephen\assembly64\Archives\...
```

Examples:

- `*.d64` search returns ~25.5k results.
- `Zap-Studio.1.6.0.exe` found at:
  `C:\Users\Stephen\Downloads\Zap-Studio.1.6.0.exe`
- Epyx/Fastload cartridge-like paths found under:
  `C:\Users\Stephen\assembly64\Archives\Tools\Ultimate\Ultimate_3.10*\carts`
  and `C:\Users\Stephen\Documents\U2P\carts`

Filename issues discovered:

- URL-encoded filenames like:

```text
%281476%29 BODSQUAD.D64 -> (1476) BODSQUAD.D64
4th%26inches.d64 -> 4th&inches.d64
```

- Archive may contain names/path data that are awkward/illegal on Windows.
- Do not rename archive in place.
- Future local/NAS index should store separately:

```text
raw filesystem name/path
decoded display name
normalized search title
safe promoted filename
source path
size/hash/source metadata
```

## Future provider/NAS direction

Likely future branch: local/NAS A64-compatible provider/server.

Idea:

```text
Curator -> provider abstraction -> Public A64 / Local NAS A64 / filesystem archive / Everything index
```

Near-term likely path:

1. Move/copy local Assembly64 archive to NAS.
2. Run an A64-compatible local HTTP server against it, possibly based on Spiffy Home Assembly64.
3. Point existing `Assembly64Client` at it with `--base-url` / provider selector.
4. Prefer local/NAS provider by default, public A64 as optional fallback.
5. Avoid U2 reaching internet directly; curator/NAS handles external/local source access, then pushes to U2.

Reasoning:

- Show sites may have no internet.
- U2 should remain isolated/listening only, not depending on outbound internet.
- 100Mbit U2 FTP/API upload is practical bottleneck; LAN/NAS download speed is less important for D64/D71/D81 sizes.

## Fastload cartridge future branch

After committing A64 branch and returning to main, discuss/plan optional fast-load cartridge behavior:

- Optional cartridge attach before disk-image launch.
- Not necessarily Epyx FastLoad only.
- Need U2 API investigation for cartridge mount/config.
- Must interact safely with existing cartridge-clear-before-reset behavior.
- Should be explicit per-image/global setting, not inferred from path.

## Keep in mind before commit

Likely inspect/stage intentionally:

- `.gitignore`
- `ASSEMBLY64_API_NOTES.md`
- `u2_assembly64.py`
- `u2_curate_tui.py`
- `u2_common.py`
- `u2_inventory.py`
- `sql/a64_downloads.sql`
- `tests/test_a64_sort.py`
- `scripts/attach-nfc-wsl.sh`
- maybe docs/notes touched intentionally

Do not accidentally commit private/generated/local content:

- `curator.db*`
- `a64_inbox/`
- `test/`
- `image/`
- `tmp_keytest.py`
- `merge_logs/`
- local disk images/PRGs
