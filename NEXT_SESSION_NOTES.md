# NFC / Ultimate-II+ Curator - Next Session Notes

## Project Identity
This is **not just a game manager**. It is an **NFC curator/launcher** for Commodore content on an Ultimate-II+/U2+ target. Most content is games, but utilities, demos, print software, and other disk/PRG/CRT content should be treated as first-class curated launch targets.

Core design remains:

- One NFC reader
- One launcher process
- One Ultimate/U2+ target
- NFC cards usually contain only a simple path payload, e.g.
  ```text
  U2+:/C64/Games/Hot Wheels/Hot Wheels (Epyx)[7786].d71
  ```
- PC-side database/config decides launch mode, target mode, entry, scripts, help, status, etc.
- USB paths are abstracted; tag/config paths should not rely on `/Usb0` or `/Usb1`.

## Current Machine / Environment
Working directory on Midnight:

```text
/home/stephen/nfc
```

Current Ultimate IP most recently:

```text
192.168.4.99
```

Common files:

```text
u2_common.py
u2_curate_tui.py
u2_inventory.py
u2_tag_monitor.py
nfc_write_text.py
rest_key_game_candidates.tsv
curator_state.tsv
approved_games.csv / approved_games.tsv legacy/import files
scripts/
harness/
NEXT_SESSION_NOTES.md
```

The user is likely moving the full directory/project content from Midnight to their main rig, then later will set up git on Midnight to pull source/config back down.

## Important Recent Decisions

### Disk Launch Mode
Use generic:

```text
disk
```

not `d64`, in UI/data. Code may still accept old `d64` as an alias.

Disk launch behavior:

- blank `entry` means:
  ```basic
  LOAD"*",8,1
  RUN
  ```
- nonblank `entry` means:
  ```basic
  LOAD"<entry>",8,1
  RUN
  ```
- In edit flow, entering `*` clears entry back to blank/default.

### Scanner / Inventory Entry Behavior
The inventory scanner previously guessed the first PRG in multi-PRG D64s as the disk entry. That was wrong.

Current expected behavior:

- single-PRG D64 may be suggested as `mode=prg` with entry
- multi-PRG D64 should stay:
  ```text
  mode=disk
  entry=
  ```
- named entry should be explicitly curated by user.

### Keyboard Buffer Injection Bug
A major weird-color issue was traced to keyboard buffer overrun.

Symptoms:

- named LOAD entries longer than short default command caused random-looking colors / editor weirdness
- e.g. MonopolyDeluxe with `entry=monopoly deluxe`

Cause:

- old code shoved the entire LOAD line into the small C64/C128 keyboard buffer.

Fix:

- `inject_keys()` in `u2_common.py` now chunks all keyboard injection safely and polls keyboard-buffer count to drain between chunks.
- C64 count byte: `$00C6`
- C128 count byte: `$00D0`
- C64 buffer: `$0277`
- C128 buffer: `$034A`

Do not regress this. All Commodore keyboard text should go through the generalized `inject_keys()` / `send_script_keys()` path.

## Prehelp System Current State
Prehelp is intended to show user instructions **after LOAD completes but before RUN**.

Script format example:

```text
[/C64/Games/Sonic/Sonic.d81]
_
{CLR}{WHITE}SONIC THE HEDGEHOG

{LTRED}IMPORTANT:
{WHITE}Choose {GREEN}Y{WHITE} when asked.

{LTBLUE}Press RETURN to start.
{HIDE}run
```

Markers:

```text
_
prehelp
_prehelp
```

Once marker appears, rest of that section is literal screen text, not script commands. `#` after marker is displayed as text, not comment.

Supported tags:

```text
{CLR}
{HOME}
{HIDE}
{BLACK}
{WHITE}
{RED}
{CYAN}
{PURPLE}
{GREEN}
{BLUE}
{YELLOW}
{ORANGE}
{BROWN}
{LTRED}
{DKGRAY}
{GRAY}
{LTGREEN}
{LTBLUE}
{LTGRAY}
```

Current strategy:

- Do **not** poke `$D018` for mixed case anymore.
- PC-side prehelp text is lowercased before rendering so it appears as uppercase in normal Commodore charset.
- This avoids disturbing loaded programs that may depend on `$D018`.
- Prehelp rendering writes screen/color memory in small safe chunks via `write_mem()`.
- `U2_WRITEMEM_CHUNK` default is 40 due to likely U2 URL length limits.

Prehelp load wait:

- Do not render help during active load.
- Wait for post-LOAD `READY` using lightweight row scan, not full 1KB screen scrape.
- Defaults:
  ```text
  U2_PREHELP_READY_TIMEOUT=180
  U2_PREHELP_READY_INTERVAL=2.0
  U2_PREHELP_READY_ROW=10
  ```

Prehelp line reveal:

```text
U2_PREHELP_LINE_DELAY=0.5
```

Known caveat:

- Post-LOAD prehelp writes to screen RAM (`$0400-$07E7`). This is usually fine at BASIC READY but may still be unsafe for some loaders/content. `T` test bypasses prehelp.

## TUI Current Features
File:

```text
u2_curate_tui.py
```

Key features implemented:

- `/` plain text search/filter
- empty search clears filter
- no regex
- if no matches, shows dim:
  ```text
  <No files found for search>
  ```
- PgUp/PgDn for list
- Home/End for list
- `?` help screen scrolls with PgUp/PgDn/Home/End
- Command panel on right; collapses to just `? help` when terminal too short
- bottom panels use full width if not overlapping command panel
- `$` on selected disk shows directory listing via local `c1541`
- In `e` edit flow, typing `$` at entry prompt opens directory picker and can select PRG entry.
- `M` mounts selected disk image to drive A/8.
- `G` sends GO64/Y manually.
- `T` test launch bypasses prehelp.
- `t` normal test launch.

### Directory Viewer / Picker
`$` downloads selected disk image temporarily via FTP and runs:

```bash
c1541 image -list
```

Picker mode in entry edit:

- Up/Down moves selection like main list
- PgUp/PgDn page
- Home/End first/last
- Enter selects highlighted PRG
- Non-PRGs dimmed and cannot be selected

## Inventory / Rescan
File:

```text
u2_inventory.py
```

Current default now appends into:

```text
rest_key_game_candidates.tsv
```

not `inventory.csv`.

Behavior:

- FTP navigation/listing
- scans supported files:
  ```text
  .d64 .d71 .d81 .prg .crt
  ```
- append-only by exact normalized path
- preserves existing curated rows
- strips `/Usb0` and `/Usb1`

Useful modes:

```bash
./u2_inventory.py
```

Scan selected image’s directory only:

```bash
./u2_inventory.py --selected-path "/C64/Games/Hot Wheels/Hot Wheels (Epyx)[7786].d71"
```

Scan explicit directory only:

```bash
./u2_inventory.py --dir "/C64/Games/Hot Wheels"
```

This is intended to support future converter workflow: after creating/uploading a new D71/D81, rescan only that directory and append the new file.

## One-Time Import / State
Files:

```text
rest_key_game_candidates.tsv  # golden-ish candidate inventory
curator_state.tsv             # active curated state
approved_games.csv/tsv        # legacy/import/export files
```

Current startup behavior in TUI:

- `curator_state.tsv` is normal persistent state.
- approved manifest is treated as one-time import source only if state is missing or `--import-approved` is specified.
- if imported, approved source is renamed:
  ```text
  approved_games.csv.imported-YYYYMMDD-HHMMSS
  ```

The goal is to avoid repeatedly re-importing stale generated entries and corrupting the curated working state.

## Disk Combining / Conversion
Important harness:

```text
harness/combine_disks.py
```

It combines normal CBM DOS image files into a larger image via `c1541`.

Examples:

```bash
python3 harness/combine_disks.py \
  -s side1.d64 side2.d64 \
  -t combined.d71 \
  --overwrite
```

Supports target types via extension:

```text
.d71
.d81
.d80
.d82
```

Prevents down-converts based on rank. D64+D64 -> D71/D81 is okay. D71 -> D64 is not.

Recent fixes:

- made executable
- fixed raw docstring warning
- handles CBM filenames beginning with `-` by using interactive c1541 command quoting
- default label now uses first source disk title/id if not supplied

### Hot Wheels Success
Uploaded originals to U2:

```text
/Usb0/C64/Games/Hot Wheels/Hot Wheels (Epyx)(A)[7786].d64
/Usb0/C64/Games/Hot Wheels/Hot Wheels (Epyx)(B)[7786].d64
```

Combined D71 created and uploaded:

```text
/Usb0/C64/Games/Hot Wheels/Hot Wheels (Epyx)[7786].d71
```

Confirmed working on U2/C128.

This is now an important workflow goal: combine two D64 sides into D71 to avoid slow 1541 and disk-swap cards.

## Future High Priority Work

### 1. View / Filter System
User suggested `Ctrl-V` for View mode.

Potential filters:

- D64 only
- D71 only
- D81 only
- PRG only
- CRT only
- C64 only
- C128 only
- approved only
- unapproved only
- failed only
- quarantined content
- utilities only
- games only
- demos only

For now can be in-memory TSV filters. Long-term may want SQLite.

### 2. SQLite Migration
User is considering SQLite because TSV is getting stretched.

Do not call tables `games` only; this is an NFC curator for many content types.

Potential schema naming:

```sql
items
item_tags
launch_profiles
conversions
conversion_sources
nfc_cards
```

Important concepts:

- path unique
- launch mode
- machine mode
- entry
- status
- category/type (game, utility, demo, print, etc.)
- tags
- notes
- quarantine/hidden flag
- conversion lineage

### 3. Merge Workflow in TUI
Need multi-select and conversion action.

Desired:

- select multiple disk images
- choose target type D71/D81
- run `harness/combine_disks.py`
- upload output to same/specified U2 directory
- rescan selected directory only
- append new candidate

Initial priority likely:

```text
D64 + D64 -> D71
```

### 4. Basic File Management
Likely later:

- rename
- move/quarantine
- delete perhaps much later / safer with confirmation

## Tooling / Editor
Installed:

```text
micro
mc / mcedit
```

`~/.bashrc` has:

```bash
export EDITOR=mcedit
```

mcedit is preferred for script editing for now.

## Important API / U2 Notes
Confirmed routes:

```text
GET  /v1/version
GET  /v1/drives
PUT  /v1/drives/a:mount?image=...
PUT  /v1/drives/a:remove
PUT  /v1/machine:reset
PUT  /v1/machine:reboot
GET  /v1/machine:readmem
PUT  /v1/machine:writemem?address=...&data=...
POST /v1/runners:run_prg
POST /v1/runners:run_crt
```

Important: on this U2+, `POST /v1/machine:writemem` can 404. Use PUT with hex `data=`.

Also keep URL length below old/small-parser limits. `write_mem()` chunks by default around 40 bytes.

G64 mount is still unsupported/blocked:

```text
HTTP 500 Illegal mount mode / drive type
```

TAP ignored/unsupported.

## Known Preferences / UX
- successful HTTP 200 with empty `errors` should not be noisy
- suppress expected `/Usb0` vs `/Usb1` fallback notes in curses/TUI where possible
- line/status colors:
  - green ready/success
  - grey work
  - yellow warnings/post waits
  - red actual errors
- red should be reserved for real errors
- TUI readable with right command panel and bottom drive status/details
- user likes preview/linter flow for prehelp
- linter should annotate offending script lines with `# Linter:` when reopening editor

## Last Known State Before This Note
- Hot Wheels D71 works and is exciting/useful.
- MonopolyDeluxe launch color issue fixed by generalized keyboard chunking.
- MonopolyDeluxe marked failed because intro is awful.
- RetaliateDX worked after clearing unintended entry.
- WeAreDemo(A) was switched to C128 for testing; C128 load path showed issues because LOAD got chunked incorrectly at 8 earlier. That should now be fixed by generalized chunking / chunk size default 10, but re-test if needed.

## Good Commands to Remember

Run curator:

```bash
./u2_curate_tui.py
```

Scan U2 and append new paths:

```bash
./u2_inventory.py
```

Scan just selected image directory:

```bash
./u2_inventory.py --selected-path "/C64/Games/Hot Wheels/Hot Wheels (Epyx)[7786].d71"
```

Combine disks:

```bash
python3 harness/combine_disks.py \
  -s side1.d64 side2.d64 \
  -t combined.d71 \
  --overwrite
```

Bypass prehelp test in TUI:

```text
T
```

Normal test:

```text
t
```

Directory view:

```text
$
```

Entry edit then directory picker:

```text
e
entry prompt: $
```
