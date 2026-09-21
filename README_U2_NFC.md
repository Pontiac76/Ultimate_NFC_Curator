# Ultimate-II+ NFC Launcher Project

This directory contains rough-draft tools for using a PN532 NFC reader attached to a PC to launch curated C64 games on an Ultimate-II+ / Ultimate family device.

Current known hardware from the show setup:

- NFC reader: PN532 via CH340 serial adapter
- Preferred serial device: `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`
- Fallback serial device: `/dev/ttyUSB0`
- Ultimate-II+ found at show on: `10.10.10.88`
- Ultimate firmware/banner observed: `Ultimate-II Plus 3.11 (11E)`
- Ultimate FTP root exposed: `Flash`, `Temp`, `Usb0`
- Useful game root: `/Usb0/C64`

Docker interfaces are unrelated and should be ignored. The scripts ignore `docker*`, `br-*`, `veth*`, and `lo` for LAN discovery.

## NFC payload format

Cards are NDEF Text records containing strings prefixed with:

```text
U2+:
```

Examples:

```text
U2+:prg:/Usb0/C64/Games/Ghostbusters/ghostbusters.d64#ghostbusters
U2+:d64:/Usb0/C64/Games/Monopoly/MonopolyDeluxe.d64#monopoly deluxe
U2+:crt:/Usb0/C64/crt/Planet_X2.1_GMod2.crt
```

Format:

```text
U2+:<mode>:<ultimate-path>[#entry]
```

Modes currently used:

- `prg` - run a PRG directly. If path is a `.d64`, extract `#entry` from the D64, then send it to `/v1/runners:run_prg`.
- `crt` - download CRT and send it to `/v1/runners:run_crt`. Confirmed working under WSL with Planet X2.1 GMod2.
- `d64` - rough fallback currently extracts `#entry` from the D64 and runs it as PRG. This worked for Monopoly in testing, but true disk mounting still needs API research.

## Ultimate discovery behavior

Shared helper: `u2_common.py`

Discovery order:

1. Try last known good host from `.u2_state.json`.
2. Scan non-Docker `/24` LAN(s) for HTTP/FTP Ultimate signatures.
3. Try fallback hosts from `u2_hosts.txt`.
4. Exit if not found.

If `u2_hosts.txt` does not exist, it will be created with starter values:

```text
10.10.10.88
192.168.1.88
192.168.0.88
```

## Existing scripts

### `nfc_listen.py`

Development/debug reader. It listens for tags and prints raw PN532 responses, UID, tag memory, NDEF bytes, and decoded text.

Run:

```bash
sudo ./nfc_listen.py
```

Under WSL, after reboot or `wsl --shutdown`, first run:

```bash
scripts/attach-nfc-wsl.sh
```

### `nfc_write_text.py`

One-shot writer. Writes a supplied string as an NDEF Text record and verifies the write.

Run:

```bash
sudo ./nfc_write_text.py 'U2+:prg:/Usb0/C64/Games/Ghostbusters/ghostbusters.d64#ghostbusters'
```

### `u2_nfc_launcher.py`

Continuously listens for NFC cards. When a `U2+:` string is read, it launches via the Ultimate API.

Run:

```bash
sudo ./u2_nfc_launcher.py --ultimate auto
```

For `d64` fallback behavior:

```bash
sudo ./u2_nfc_launcher.py --ultimate auto --d64-as-prg-loader
```

## New rough-draft workflow scripts

### 1. Inventory the Ultimate USB

```bash
./u2_inventory.py --ultimate auto --root /Usb0/C64 --out inventory.csv
```

This recursively scans `/Usb0/C64` to limited depth and writes likely launchable files to `inventory.csv`.

It looks for:

- `.d64`
- `.prg`
- `.crt`

For D64s, it downloads the image read-only over FTP, reads the directory, and suggests a launch mode/entry:

- single PRG inside D64 -> `mode=prg`, `entry=<that prg>`
- multiple PRGs -> `mode=d64`, `entry=<first prg>`
- CRT -> `mode=crt`

Output columns include:

```csv
title,path,file_type,mode,entry,payload,detail
```

### 2. Browse, test, and approve games in one TUI

```bash
./u2_curate_tui.py inventory.csv --ultimate auto --out approved_games.csv
```

Controls:

```text
Up/Down or j/k  move
t               test launch immediately on the Ultimate
Space or a       approve/unapprove
f               mark failed
u               unmark status
e               edit mode/entry and regenerate payload
s               save approved list
q               save approved list and quit
```

Intended process:

1. Scroll through games.
2. When something looks interesting, press `t`.
3. Look over at the C128/monitor.
4. If it launched acceptably, press `a`.
5. Move on.
6. Quit/save when done.

This writes only approved games to:

```text
approved_games.csv
```

### 3. Export labels for printing

```bash
./u2_export_labels.py approved_games.csv --out labels.csv
```

This writes a CSV containing only:

```csv
title
Ghostbusters
Monopoly
...
```

Use this in Excel/LibreOffice/label software. The physical labels can just be the game names.

Important: keep the physical cards in the same order as `approved_games.csv` / `labels.csv` while sticking labels to cards.

### 4. Guided card writing

```bash
sudo ./u2_write_cards_from_manifest.py approved_games.csv --ultimate auto
```

For each approved game, it shows the title and payload, optionally test-launches it again, then asks you to place the matching labeled NFC card on the reader. It calls `nfc_write_text.py` to write and verify the NFC card.

If you do not want the extra launch test during writing:

```bash
sudo ./u2_write_cards_from_manifest.py approved_games.csv --skip-launch-test
```

## Typical end-to-end flow

```bash
# 1. Scan Ultimate USB for games
./u2_inventory.py --ultimate auto --root /Usb0/C64 --out inventory.csv

# 2. Browse, test launch, and approve games
./u2_curate_tui.py inventory.csv --ultimate auto --out approved_games.csv

# 3. Export title-only labels
./u2_export_labels.py approved_games.csv --out labels.csv

# 4. Print labels.csv, cut labels, stick them to NFC cards in order

# 5. Program cards in that same order
sudo ./u2_write_cards_from_manifest.py approved_games.csv --ultimate auto

# 6. Run the live NFC launcher
sudo ./u2_nfc_launcher.py --ultimate auto --d64-as-prg-loader
```

## Known tested payloads

Ghostbusters:

```text
U2+:prg:/Usb0/C64/Games/Ghostbusters/ghostbusters.d64#ghostbusters
```

Observed D64 contents:

```text
186 blocks  ghostbusters  PRG
load address: $0801
```

Monopoly:

```text
U2+:d64:/Usb0/C64/Games/Monopoly/MonopolyDeluxe.d64#monopoly deluxe
```

Observed D64 contents included:

```text
monopoly deluxe
mon1
mon2
mon3
...
mona
```

The `d64` fallback method worked in show testing for Monopoly according to user feedback.

## Caveats / TODO

- True D64 mounting via Ultimate API still needs confirmation. Current scripts use the practical fallback of extracting the specified loader PRG from D64 and sending it to `run_prg`.
- Multi-file disk games may fail if the loader expects disk files after starting.
- TUI is intentionally rough. It is designed to be good enough for fast curation.
- Inventory scanning downloads D64 files to inspect directories. It is read-only but can take time.
- If running from home on a different subnet, edit `u2_hosts.txt` with expected Ultimate IPs if auto-scan does not find it.
