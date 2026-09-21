#!/usr/bin/env bash
set -euo pipefail

# Reattach the CH340/PN532 NFC reader to WSL after a Windows reboot,
# wsl --shutdown, unplug/replug, or USB port change.

VIDPID="${NFC_VIDPID:-1a86:7523}"
USBIPD="usbipd"
USBIPD_FULL="& 'C:\Program Files\usbipd-win\usbipd.exe'"

ps() {
  powershell.exe -NoProfile -Command "$*" | tr -d '\r'
}

run_usbipd() {
  ps "$USBIPD $*"
}

if ! ps "Get-Command usbipd -ErrorAction SilentlyContinue" | grep -qi usbipd; then
  USBIPD="$USBIPD_FULL"
fi

echo "Looking for NFC adapter $VIDPID via usbipd..."
LIST="$(run_usbipd list)"
MATCHES="$(printf '%s\n' "$LIST" | awk -v id="$VIDPID" 'tolower($0) ~ id {print}')"
BUSID="$(printf '%s\n' "$MATCHES" | awk 'tolower($0) !~ /not shared/ {print $1; exit}')"
if [ -z "${BUSID:-}" ]; then
  BUSID="$(printf '%s\n' "$MATCHES" | awk 'NF {print $1; exit}')"
fi

if [ -z "${BUSID:-}" ]; then
  echo "Could not find USB device $VIDPID in usbipd list." >&2
  echo "Plug the reader into Windows, then retry." >&2
  echo >&2
  echo "$LIST" >&2
  exit 1
fi

echo "Found BUSID: $BUSID"
if [ "$(printf '%s\n' "$MATCHES" | awk 'NF {count++} END {print count+0}')" -gt 1 ]; then
  echo "Multiple matching CH340 devices found; using first shared/attached match: $BUSID"
fi

if printf '%s\n' "$LIST" | awk -v busid="$BUSID" '$1 == busid {print}' | grep -Eiq 'Not shared|Persisted:[[:space:]]*no'; then
  echo "Device appears not to be shared/bound to usbipd." >&2
  echo "Run this once from Administrator PowerShell, then retry:" >&2
  echo "  usbipd bind --busid $BUSID" >&2
  exit 2
fi

echo "Attaching to WSL..."
if ! ATTACH_OUT="$(run_usbipd attach --wsl --busid "$BUSID" 2>&1)"; then
  if printf '%s\n' "$ATTACH_OUT" | grep -Eiq 'already attached'; then
    echo "Already attached. Continuing checks..."
  else
    echo "$ATTACH_OUT" >&2
    if printf '%s\n' "$ATTACH_OUT" | grep -Eiq 'not shared|bind'; then
      echo >&2
      echo "If needed, run this once from Administrator PowerShell:" >&2
      echo "  usbipd bind --busid $BUSID" >&2
    fi
    exit 3
  fi
else
  printf '%s\n' "$ATTACH_OUT"
fi

sleep 1

echo
echo "Linux USB check:"
lsusb | grep -i "$VIDPID" || true

echo
echo "Serial device check:"
ls -l /dev/serial/by-id/* /dev/ttyUSB* 2>/dev/null || {
  echo "No /dev/ttyUSB* device visible yet. Try unplug/replug, then rerun this script." >&2
  exit 4
}

echo
echo "NFC reader should now be usable. Preferred device:"
if [ -e /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 ]; then
  echo "  /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
else
  echo "  /dev/ttyUSB0"
fi
