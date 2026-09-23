# Ultimate NFC Curator Setup

Single-pane setup notes for fresh systems.

This project has two common environments:

1. **WSL development machine** — Windows + WSL2 + USB passthrough for the PN532/CH340 NFC reader.
2. **Raspberry Pi / Debian appliance** — eventually the show-floor launcher box with native USB serial access to the reader.

The Python runtime currently uses only the Python standard library. OS-level tools are still required.

## Repository checkout

```bash
git clone <repo-url>
cd Ultimate_NFC_Curator
```

If this repo is already checked out:

```bash
cd ~/git/GitHub/Ultimate_NFC_Curator
```

## Fresh Raspberry Pi / Debian setup

Run:

```bash
scripts/setup-pi.sh
```

This installs the baseline OS packages:

```text
python3
python3-pip
python3-venv
sqlite3
vice        # provides c1541
usbutils
git
```

It also installs Python requirements from:

```text
requirements.txt
```

Current Python package requirements: none beyond the standard library.

The script also adds the current user to `dialout` so `/dev/ttyUSB*` can be opened without `sudo`.

If the script adds your user to `dialout`, log out/in or reboot before using NFC tools.

Verify:

```bash
id | grep dialout
which sqlite3
which c1541
lsusb
ls -l /dev/serial/by-id/* /dev/ttyUSB* 2>/dev/null
```

Expected NFC reader hardware:

```text
VID:PID 1a86:7523
CH340 USB serial bridge
PN532 NFC controller behind serial bridge
```

Preferred reader path:

```text
/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
```

Fallback:

```text
/dev/ttyUSB0
```

## Fresh WSL development setup

Detailed WSL notes are in:

```text
WSL_Reader_Info.md
```

Short version:

1. Install `usbipd-win` on Windows.
2. Bind/share the CH340 device once from Administrator PowerShell if needed.
3. Attach it to WSL after reboot or `wsl --shutdown`.

From WSL, after the reader is plugged into Windows:

```bash
scripts/attach-nfc-wsl.sh
```

Verify:

```bash
lsusb | grep -i '1a86:7523'
ls -l /dev/serial/by-id/* /dev/ttyUSB*
id | grep dialout
```

If WSL reports the device is not shared/bound, run the printed command from Administrator PowerShell, then rerun:

```bash
scripts/attach-nfc-wsl.sh
```

## First database population

The database file is local runtime state and is intentionally ignored by Git:

```text
curator.db
```

To populate from the Ultimate USB:

```bash
./u2_inventory.py
```

Default scan root:

```text
/
```

Normal inventory scans are path-only and fast-ish. They do **not** download every disk image.

To scan a specific root:

```bash
./u2_inventory.py --root /C64
./u2_inventory.py --root /C128
```

To explicitly download D64/D71/D81 images and inspect directories with `c1541`:

```bash
./u2_inventory.py --inspect-disks
```

Use `--inspect-disks` sparingly on large USB trees.

## Launching the curator TUI

```bash
./curator
```

`curator` is a symlink to:

```text
u2_curate_tui.py
```

If `curator.db` exists but contains no images, the TUI will ask whether to run inventory first.

## Normal show/dev flow

Typical fresh flow:

```bash
scripts/setup-pi.sh          # Pi/Debian only
scripts/attach-nfc-wsl.sh    # WSL only
./u2_inventory.py
./curator
```

Typical recurring WSL flow after reboot:

```bash
scripts/attach-nfc-wsl.sh
./curator
```

## Important local files

Ignored/local runtime files:

```text
curator.db
curator.db.*
.u2_state.json
u2_hosts.txt
*.log
```

`u2_hosts.txt` is local configuration. It can contain fallback Ultimate host/IP values.

Example:

```text
# One host/IP per line
192.168.4.99
10.10.10.88
```

## SQLite layout

The friendly query surface is:

```sql
image_rows
```

The normalized core table is:

```sql
Image
```

Supporting lookup/join tables currently include:

```text
FileType
ImageTag
LaunchMode
MachineMode
ScanPath
Status
StorageStatus
Tag
```

Custom SQL view files live in:

```text
sql/*.sql
```

The TUI `V` key opens the SQL view picker.

## NFC reader commands

Listen/debug:

```bash
./nfc_listen.py
```

Write one text payload:

```bash
./nfc_write_text.py 'U2+:disk:/C64/Games/example.d64'
```

In normal Linux/Pi use, these should not need `sudo` if the user is in `dialout`.

## Required external tools

### sqlite3

Used for DB inspection and fast scan-path imports when available.

Check:

```bash
which sqlite3
sqlite3 --version
```

### c1541

Provided by VICE. Used for optional Commodore disk-image directory inspection and PRG extraction from disk images.

Check:

```bash
which c1541
c1541 -version
```

On Debian/Raspberry Pi OS, this should come from:

```bash
sudo apt install vice
```

If `vice` cannot be found, repository components may need adjustment. See Debian/Raspberry Pi OS package source setup.

## Future Pi display work

Companion display design notes are in:

```text
PI_COMPANION_DISPLAY_DESIGN.md
```

When the Raspberry Pi display path is implemented, this setup document should be updated with any required packages and boot/startup steps, especially for minimal Pi installs.

## Troubleshooting

### NFC reader not visible

```bash
lsusb | grep -i '1a86:7523'
ls -l /dev/serial/by-id/* /dev/ttyUSB* 2>/dev/null
```

On WSL, rerun:

```bash
scripts/attach-nfc-wsl.sh
```

On Pi/Debian, ensure user is in `dialout` and relogin/reboot:

```bash
sudo usermod -aG dialout "$USER"
```

### Empty curator DB

Run:

```bash
./u2_inventory.py
```

Then:

```bash
./curator
```

### `c1541` missing

Run:

```bash
scripts/setup-pi.sh
```

or manually:

```bash
sudo apt update
sudo apt install vice
```

### WSL USB attach lost after reboot

Expected. Run:

```bash
scripts/attach-nfc-wsl.sh
```
