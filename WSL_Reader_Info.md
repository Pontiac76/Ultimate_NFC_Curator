# WSL2 NFC Reader / PN532 USB Setup Notes

These notes describe how this Windows + WSL2 machine was made to access the NFC reader for software development.

## Hardware discovered

The reader does **not** appear as a PC/SC / CCID smart-card reader. It appears as a USB serial adapter:

```text
USB VID:PID: 1a86:7523
Description: QinHeng Electronics CH340 serial converter
Linux device: /dev/ttyUSB0
Stable Linux path: /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
```

Behind that CH340 serial bridge is a **PN532 NFC controller** using PN532 UART framing at **115200 baud**.

A successful firmware probe returned:

```text
PN532 firmware response: d5 03 32 01 06 07
IC: PN532
Firmware: 1.6
```

A card was successfully detected with UID:

```text
04 41 6d 41 2b 02 89
```

First full WSL tooling run was successful: NFC-driven launch worked, including a confirmed CRT launch of **Planet X2.1 GMod2**.

## Important behavior

USB passthrough into WSL works, but the active attachment generally does **not** survive:

- `wsl --shutdown`
- Windows reboot
- unplug/replug
- sometimes changing USB ports

After those events, reattach the device from Windows/PowerShell or from WSL by invoking PowerShell.

## Windows prerequisites

Install `usbipd-win` on Windows:

```powershell
winget install usbipd
```

Then list USB devices:

```powershell
usbipd list
```

Find the CH340 device, likely showing `1a86:7523`.

One-time bind/share, normally from **Administrator PowerShell**:

```powershell
usbipd bind --busid <BUSID>
```

Attach to WSL:

```powershell
usbipd attach --wsl --busid <BUSID>
```

## WSL prerequisites

Install basic tools:

```bash
sudo apt update
sudo apt install -y usbutils pcscd pcsc-tools libccid
```

Add the WSL user to `dialout` so it can open `/dev/ttyUSB0`:

```bash
sudo usermod -aG dialout "$USER"
```

Then restart WSL from Windows:

```powershell
wsl --shutdown
```

Reopen WSL and verify:

```bash
id
lsusb
ls -l /dev/ttyUSB* /dev/serial/by-id/*
```

Expected WSL output after attach:

```text
Bus 001 Device 002: ID 1a86:7523 QinHeng Electronics CH340 serial converter
/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 -> ../../ttyUSB0
/dev/ttyUSB0
```

## Attach from inside WSL

WSL can run Windows PowerShell commands via `powershell.exe`. This lets development workflows reattach the NFC reader from inside WSL.

Manual command:

```bash
powershell.exe -NoProfile -Command "usbipd list"
powershell.exe -NoProfile -Command "usbipd attach --wsl --busid <BUSID>"
```

If `usbipd` is not on PATH from WSL-launched PowerShell, use the full path:

```bash
powershell.exe -NoProfile -Command "& 'C:\Program Files\usbipd-win\usbipd.exe' list"
```

## Bootstrap after Windows/WSL reboot

This repo includes:

```bash
scripts/attach-nfc-wsl.sh
```

Run it after Windows reboot, `wsl --shutdown`, unplug/replug, or changing USB ports:

```bash
cd ~/git/GitHub/Ultimate_NFC_Curator
scripts/attach-nfc-wsl.sh
```

If the script reports that the device is not shared/bound, run the printed `usbipd bind --busid ...` command once from **Administrator PowerShell**, then rerun the script from WSL.

## Suggested helper script

The checked-in `scripts/attach-nfc-wsl.sh` is based on this pattern:

```bash
#!/usr/bin/env bash
set -euo pipefail

VIDPID="1a86:7523"
USBIPD="usbipd"

run_usbipd() {
  powershell.exe -NoProfile -Command "$USBIPD $*" | tr -d '\r'
}

# Fallback if usbipd is not in PowerShell PATH.
if ! powershell.exe -NoProfile -Command "Get-Command usbipd -ErrorAction SilentlyContinue" | grep -qi usbipd; then
  USBIPD="& 'C:\Program Files\usbipd-win\usbipd.exe'"
fi

echo "Looking for NFC adapter $VIDPID..."
LIST="$(run_usbipd list)"
BUSID="$(printf '%s\n' "$LIST" | awk -v id="$VIDPID" 'tolower($0) ~ id {print $1; exit}')"

if [ -z "${BUSID:-}" ]; then
  echo "Could not find USB device $VIDPID in usbipd list." >&2
  echo "$LIST" >&2
  exit 1
fi

echo "Found BUSID: $BUSID"
echo "Attaching to WSL..."
run_usbipd attach --wsl --busid "$BUSID"

sleep 1
lsusb | grep -i "$VIDPID" || true
ls -l /dev/ttyUSB* /dev/serial/by-id/* 2>/dev/null || true
```

Make executable:

```bash
chmod +x scripts/attach-nfc-wsl.sh
```

Run after reboot / WSL restart:

```bash
scripts/attach-nfc-wsl.sh
```

## Development interface

Treat the reader as a serial PN532 device, not PC/SC.

Use:

```text
Port: /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
Fallback port: /dev/ttyUSB0
Baud: 115200
Protocol: PN532 UART serial frames
```

`pcsc_scan` is expected to show no reader because this device is not exposing itself as a CCID smart-card reader.

## Minimal PN532 probe sequence that worked

Wake/SAM config frame sent at 115200 baud:

```text
55 55 00 00 00 00 00 ff 03 fd d4 14 01 17 00
```

Expected response:

```text
00 00 ff 00 ff 00 00 00 ff 02 fe d5 15 16 00
```

Get firmware frame:

```text
00 00 ff 02 fe d4 02 2a 00
```

Expected response observed:

```text
00 00 ff 00 ff 00 00 00 ff 06 fa d5 03 32 01 06 07 e8 00
```

Poll ISO14443A card frame:

```text
00 00 ff 04 fc d4 4a 01 00 e1 00
```

Successful card response observed:

```text
00 00 ff 00 ff 00 00 00 ff 0f f1 d5 4b 01 01 00 44 00 07 04 41 6d 41 2b 02 89 ea 00
```

## Notes for future implementation

A software package should:

1. Provide/configure serial port path, defaulting to `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`.
2. Use 115200 baud, 8N1.
3. Initialize PN532 with SAMConfiguration.
4. Poll cards with `InListPassiveTarget` for ISO14443A.
5. Not depend on PC/SC for this reader.
6. Optionally include a WSL attach helper for development only.
