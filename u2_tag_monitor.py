#!/usr/bin/env python3
"""Standalone NFC tag monitor for Ultimate-II+ launches.

Reads NDEF text tags. Preferred card payload is path-only:
  U2+:/C64/Games/Ghostbusters/ghostbusters.d64

Launch settings come from curator_state.tsv / approved_games.tsv when present.
Press q (or Ctrl-C) to exit when run interactively.
"""

import argparse
import contextlib
import io
import os
import select
import sys
import termios
import time
from pathlib import Path

from u2_common import (
    boot_mount_load_run,
    clean_temp,
    discover_ultimate,
    ftp_download,
    launch_payload,
    post_runner,
    read_csv,
    run_launch_script,
    strip_usb_prefix,
)
from u2_nfc_launcher import (
    decode_first_ndef_text_or_uri,
    find_tag,
    open_serial,
    parse_ndef_tlv,
    read_ntag_memory,
    require_response,
)


ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[92m"
ANSI_GREY = "\033[37m"
ANSI_RED = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_CYAN = "\033[96m"


def colorize(enabled, color, text):
    return f"{color}{text}{ANSI_RESET}" if enabled else text


def state(color, label, text, enabled=True):
    # Always write monitor status to the real stdout. During live launch logging
    # stdout/stderr are redirected into LiveStatusWriter; using normal print()
    # here would recurse back into the writer.
    print(colorize(enabled, color, f"[{label}] {text}"), file=sys.__stdout__, flush=True)


def api_block_is_success(block):
    joined = " ".join(block).lower()
    return "http 200" in joined and '"errors"' in joined and "[  ]" in joined


def classify_worker_line(line, color_enabled=True):
    low = line.lower()

    # USB0/USB1 probing fallback is expected; hide it unless it actually fails.
    if line.startswith("Note:") and " failed; using /Usb" in line:
        return

    # Suppress successful empty-error JSON fragments from API responses.
    if line.strip() in ("{", "}") or '"errors" : [  ]' in line:
        return

    if "post-launch script" in low or "step script" in low or "waitfor" in low:
        state(ANSI_YELLOW, "POST", line, color_enabled)
    elif "error" in low or "failed" in low:
        state(ANSI_RED, "ERROR", line, color_enabled)
    elif "warning" in low:
        state(ANSI_YELLOW, "WARN", line, color_enabled)
    elif line.startswith("STEP") or line.startswith("TYPE") or line.startswith("Note:"):
        state(ANSI_GREY, "WORK", line, color_enabled)
    else:
        state(ANSI_GREY, "WORK", line, color_enabled)


def replay_worker_output(text, color_enabled=True):
    for line in text.splitlines():
        classify_worker_line(line, color_enabled)


class LiveStatusWriter:
    """File-like stdout/stderr adapter that colorizes lines as they happen."""
    def __init__(self, color_enabled=True):
        self.color_enabled = color_enabled
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line:
                classify_worker_line(line, self.color_enabled)
        return len(s)

    def flush(self):
        if self.buf:
            classify_worker_line(self.buf, self.color_enabled)
            self.buf = ""


def load_launch_db(paths):
    rows = []
    for p in paths:
        if Path(p).exists():
            try:
                rows.extend(read_csv(p))
            except Exception as e:
                print(f"WARN: could not read {p}: {e}", file=sys.stderr)
    by_path = {}
    for r in rows:
        path = strip_usb_prefix(r.get("path", ""))
        if path:
            by_path[path] = r
    return by_path


def row_for_path(db, path):
    path = strip_usb_prefix(path)
    return db.get(path, {"path": path, "machine_mode": "c64"})


def launch_path(host, db, path):
    path = strip_usb_prefix(path)
    row = row_for_path(db, path)
    target = row.get("machine_mode") or "c64"
    lower = path.lower()
    print(f"Launch path={path} target={target}")

    if lower.endswith((".d64", ".d71", ".d81")):
        return boot_mount_load_run(host, path, target_mode=target, entry=row.get("entry", ""))
    if lower.endswith(".crt"):
        blob = ftp_download(host, path)
        return post_runner(host, "/v1/runners:run_crt", blob)
    if lower.endswith(".prg"):
        blob = ftp_download(host, path)
        result = post_runner(host, "/v1/runners:run_prg", blob)
        run_launch_script(host, path, target)
        return result
    raise RuntimeError(f"Unsupported NFC path type: {path}")


def launch_text(host, db, text):
    if not text.startswith("U2+:"):
        print(f"Ignoring non-U2+ tag: {text!r}")
        return
    rest = text[4:]
    # Old format compatibility: U2+:mode:/path[#entry]
    if ":" in rest and not rest.startswith("/"):
        print("Old-style payload detected; using embedded launch mode")
        status, body = launch_payload(host, text, target_mode="c64")
    else:
        status, body = launch_path(host, db, rest)
    body_text = str(body)
    if status != 200 or ('"errors"' in body_text and "[  ]" not in body_text and "[]" not in body_text):
        print(f"Ultimate response: HTTP {status} {body_text.strip()}")


def stdin_key_available():
    if not sys.stdin.isatty():
        return None
    r, _, _ = select.select([sys.stdin], [], [], 0)
    if r:
        return sys.stdin.read(1)
    return None


def open_reader_when_available(device, baud, retry=1.0):
    baud_const = getattr(termios, f"B{baud}")
    while True:
        if not os.path.exists(device):
            print(f"Waiting for NFC reader device {device}...")
            time.sleep(retry)
            continue
        try:
            fd = open_serial(device, baud_const)
            fw = require_response(fd, [0x02], 0x03, timeout=1.0)
            require_response(fd, [0x14, 0x01, 0x14, 0x01], 0x15, timeout=1.0)
            print(f"PN532 ready on {device}: {fw.hex(' ')}")
            return fd
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"NFC reader not ready: {e}")
            try:
                os.close(fd)  # type: ignore[name-defined]
            except Exception:
                pass
            time.sleep(retry)


def wait_for_tag_removal(fd, poll_delay=0.25):
    """Silently wait until the current tag is no longer detected."""
    misses = 0
    while misses < 3:
        try:
            uid = find_tag(fd)
            if uid:
                misses = 0
            else:
                misses += 1
        except Exception:
            misses += 1
        time.sleep(poll_delay)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", "-d", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200, choices=[9600, 19200, 38400, 57600, 115200])
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--state", action="append", default=["curator_state.tsv", "approved_games.tsv"], help="TSV launch DB; may be repeated")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-launch", action="store_true", help="read/print tag contents but do not launch anything")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI color/status output")
    args = ap.parse_args()

    host = discover_ultimate() if args.ultimate == "auto" else args.ultimate
    if not host:
        raise SystemExit("Could not find Ultimate-II+")
    db = load_launch_db(args.state)
    color_enabled = not args.no_color
    state(ANSI_CYAN, "INIT", f"Ultimate: {host}", color_enabled)
    state(ANSI_CYAN, "INIT", f"Loaded {len(db)} launch DB row(s)", color_enabled)
    state(ANSI_GREEN, "READY", "Monitoring NFC reader. Press q to exit.", color_enabled)

    old_term = None
    if sys.stdin.isatty():
        old_term = termios.tcgetattr(sys.stdin)
        tty_attrs = old_term[:]
        tty_attrs[3] &= ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(sys.stdin, termios.TCSANOW, tty_attrs)

    fd = None
    last_text = None
    last_seen = 0.0
    try:
        fd = open_reader_when_available(args.device, args.baud)
        state(ANSI_GREEN, "READY", "Waiting for NFC tag...", color_enabled)
        while True:
            ch = stdin_key_available()
            if ch and ch.lower() == "q":
                print("\nExiting NFC monitor.")
                return

            uid = find_tag(fd)
            if uid:
                try:
                    memory = read_ntag_memory(fd)
                    text = decode_first_ndef_text_or_uri(parse_ndef_tlv(memory))
                    now = time.time()
                    if text and (text != last_text or now - last_seen > 3.0):
                        print()
                        state(ANSI_CYAN, "TAG", f"UID {uid.hex(':')} text: {text}", color_enabled)
                        if not args.no_launch:
                            state(ANSI_GREY, "WORK", "Pre-cleaning Ultimate /Temp...", color_enabled)
                            live_out = LiveStatusWriter(color_enabled)
                            live_err = LiveStatusWriter(color_enabled)
                            with contextlib.redirect_stdout(live_out), contextlib.redirect_stderr(live_err):
                                try:
                                    clean_temp(host)
                                except Exception as e:
                                    print(f"STEP clean temp warning: {e}")
                                print("STEP launching selected item")
                                launch_text(host, db, text)
                            live_out.flush()
                            live_err.flush()
                        last_text = text
                        last_seen = now
                        if args.once:
                            return
                        wait_for_tag_removal(fd)
                        last_text = None
                        state(ANSI_GREEN, "READY", "Waiting for NFC tag...", color_enabled)
                    elif not text:
                        print()
                        state(ANSI_YELLOW, "TAG", f"UID {uid.hex(':')}: no data present", color_enabled)
                        last_seen = now
                        if args.once:
                            return
                        wait_for_tag_removal(fd)
                        last_text = None
                        state(ANSI_GREEN, "READY", "Waiting for NFC tag...", color_enabled)
                except Exception as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                    last_seen = time.time()
            elif time.time() - last_seen > 1.0:
                last_text = None
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nExiting NFC monitor.")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if old_term is not None:
            termios.tcsetattr(sys.stdin, termios.TCSANOW, old_term)


if __name__ == "__main__":
    main()
