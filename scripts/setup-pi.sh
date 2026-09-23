#!/usr/bin/env bash
set -euo pipefail

# Raspberry Pi / Debian system prep for Ultimate NFC Curator.
# Installs OS packages used by the curator, NFC serial tooling, SQLite, and
# Commodore disk-image inspection. Safe to rerun.

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
Usage: scripts/setup-pi.sh

Prepares Raspberry Pi OS / Debian for Ultimate NFC Curator:
  - installs python3, sqlite3, vice/c1541, usbutils, git
  - installs Python requirements.txt
  - adds the current user to dialout for /dev/ttyUSB* access

Safe to rerun.
EOF
  exit 0
fi

PACKAGES=(
  python3
  python3-pip
  python3-venv
  sqlite3
  vice
  usbutils
  git
)

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This setup script expects a Debian/Raspberry Pi OS system with apt-get." >&2
  exit 1
fi

echo "Updating apt package lists..."
sudo apt-get update

echo "Installing system packages: ${PACKAGES[*]}"
sudo apt-get install -y "${PACKAGES[@]}"

if [ -f requirements.txt ]; then
  echo "Installing Python requirements..."
  python3 -m pip install --user -r requirements.txt
fi

if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
  echo "User $USER is already in dialout."
else
  echo "Adding $USER to dialout so NFC serial devices can be opened without sudo..."
  sudo usermod -aG dialout "$USER"
  NEED_RELOGIN=1
fi

echo
echo "Tool checks:"
python3 --version || true
sqlite3 --version || true
if command -v c1541 >/dev/null 2>&1; then
  c1541 -version 2>&1 | head -1 || true
else
  echo "WARN: c1541 not found. Disk directory inspection will be limited."
fi

echo
echo "NFC serial device check, if reader is plugged in:"
lsusb | grep -Ei '1a86:7523|ch340|qin' || true
ls -l /dev/serial/by-id/* /dev/ttyUSB* 2>/dev/null || true

echo
echo "Setup complete."
if [ "${NEED_RELOGIN:-0}" = "1" ]; then
  echo "IMPORTANT: log out/in or reboot before running NFC tools so dialout membership applies."
fi
