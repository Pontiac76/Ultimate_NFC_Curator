#!/usr/bin/env python3
"""Rough curses curator: browse inventory, test launch, approve, persist SQLite state."""

import argparse
import contextlib
import curses
import io
import os
import re
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path
from u2_common import *
from u2_assembly64 import Assembly64Client, build_aql, db_connect as a64_db_connect, ensure_a64_tables, upsert_result as a64_upsert_result, upsert_entry as a64_upsert_entry, record_entries_check as a64_record_entries_check, safe_inbox_filename, entry_type_summary, file_set_label, LAUNCHABLE_TYPES, REFERENCE_ONLY_TYPES, DEFAULT_INBOX
try:
    from u2_nfc_launcher import open_serial, require_response, find_tag, read_ntag_memory, parse_ndef_tlv, decode_first_ndef_text_or_uri
except Exception:
    open_serial = require_response = find_tag = read_ntag_memory = parse_ndef_tlv = decode_first_ndef_text_or_uri = None

FIELDS = ["title", "payload", "mode", "path", "entry", "machine_mode", "file_type", "detail", "status", "notes"]
UNSUPPORTED_EXTS = (".tap",)
UNSUPPORTED_TYPES = {"tap"}
# Defensive byte viewer replacement glyph. Swap if terminal font renders oddly:
# SAFE_BYTE_DOT = "·"  # U+00B7 MIDDLE DOT
# SAFE_BYTE_DOT = "∙"  # U+2219 BULLET OPERATOR
SAFE_BYTE_DOT = "⋅"  # U+22C5 DOT OPERATOR
# SAFE_BYTE_DOT = "⸱"  # U+2E31 WORD SEPARATOR MIDDLE DOT
TUI_STATE_FILE = Path(".u2_curate_tui_state.json")

A64_INBOX_ORDER_BY = """
ORDER BY
  CASE WHEN COALESCE(entries_count, 1) >= 2 THEN 1 ELSE 0 END,
  CASE
    WHEN COALESCE(entries_count, 1) >= 2 THEN lower(COALESCE(NULLIF(result_title, ''), NULLIF(a64_name, ''), title))
    ELSE lower(replace(replace(replace(title,
      ' - Converted [to D64]', ''),
      ' - Converted [to D71]', ''),
      ' - Converted [to D81]', ''))
  END,
  CASE
    WHEN instr(a64_id, '-local-') > 0 THEN substr(a64_id, 1, instr(a64_id, '-local-') - 1)
    ELSE a64_id
  END,
  a64_category,
  entry_index,
  CASE file_type
    WHEN 'prg' THEN 0
    WHEN 'd64' THEN 1
    WHEN 'd71' THEN 2
    WHEN 'd81' THEN 3
    WHEN 'crt' THEN 4
    WHEN 'tap' THEN 5
    ELSE 9
  END,
  title COLLATE NOCASE
"""


def load_tui_state():
    try:
        return json.loads(TUI_STATE_FILE.read_text())
    except Exception:
        return {}


def save_tui_state(**updates):
    state = load_tui_state()
    state.update(updates)
    TUI_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def is_supported_row(row):
    path = row.get("path", "").lower()
    typ = (row.get("type") or row.get("file_type") or "").lower()
    mode = row.get("mode", "").lower()
    if typ in UNSUPPORTED_TYPES or mode in UNSUPPORTED_TYPES:
        return False
    if path.endswith(UNSUPPORTED_EXTS):
        return False
    return True


def clean_msg(s):
    """Keep status messages curses-friendly: one printable line, no control chars."""
    s = str(s).replace("\r", " ").replace("\n", " ")
    return "".join(ch if (ch == "\t" or ord(ch) >= 32) else "?" for ch in s)


def commit_rows(rows, db_path):
    """Commit the current curator rows to SQLite immediately after a completed edit."""
    write_csv(db_path, rows, FIELDS)
    return len(rows)


def set_image_status(db_path, row, status):
    path = row.get("path", "")
    if not path:
        return False
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        conn.execute(
            "UPDATE Image SET fk_Status_ID=?, updated_at=CURRENT_TIMESTAMP WHERE path=?",
            (lookup_id(conn, "Status", status), path),
        )
    return True


def set_image_machine_mode(db_path, row, machine_mode):
    path = row.get("path", "")
    if not path:
        return False
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        conn.execute(
            "UPDATE Image SET fk_MachineMode_ID=?, updated_at=CURRENT_TIMESTAMP WHERE path=?",
            (lookup_id(conn, "MachineMode", machine_mode), path),
        )
    return True


def nfc_payload_for_row(row):
    # New card format: keep card simple; PC-side config decides launch mode.
    # Security prefix remains U2+: but no mode is encoded.
    return "U2+:" + strip_usb_prefix(row.get("path", ""))


def color_text(color, text):
    colors = {
        "green": "\033[92m",
        "red": "\033[91m",
        "yellow": "\033[93m",
        "reset": "\033[0m",
    }
    return colors[color] + text + colors["reset"]


def drive_status_rows(host):
    data = api_get_json(host, "/v1/drives")
    labels = {"a": "A", "b": "B", "IEC Drive": "S"}
    rows = []
    for item in data.get("drives", []):
        for key, info in item.items():
            if key == "Printer Emulation":
                continue
            label = labels.get(key, key)
            bus = str(info.get("bus_id", "?"))
            enabled = bool(info.get("enabled", False))
            dtype = info.get("type", "")
            mounted = info.get("image_file") or info.get("image_path") or ""
            last_error = info.get("last_error", "") or ""
            rows.append((label, bus, enabled, dtype, mounted, last_error))
    return rows


def print_drive_status(host):
    for label, bus, enabled, dtype, mounted, last_error in drive_status_rows(host):
        name = f"Drive {label}/{bus:>2}:"
        state = color_text("green", "Enabled") if enabled else color_text("red", "Disabled")
        if not enabled:
            line = f"{name} {state}"
        else:
            line = f"{name} {state} {dtype} mode"
            if mounted:
                line += f" | Mounted: {mounted}"
        if last_error:
            line += " | " + color_text("yellow", f"Error: {last_error}")
        print(line)


def command_console_help():
    print("""
Local commands begin with ] or ~ and are NOT sent to the Commodore.
Anything else is sent as keyboard input with Enter appended.

Keyboard mode:
  ]mode auto             auto-detect C64/C128 before each typed command
  ]mode c64              force C64 keyboard buffer
  ]mode c128             force C128 keyboard buffer

Machine/API:
  ]reset                 reset/cold boot machine
  ]reboot                reboot Ultimate/machine
  ]status                show flattened drive status
  ]unmount [a|b|8|9]     remove image from drive, default a
  ]mount <path> [a|b]    mount image path, default drive a

Utility:
  ]? or ~?               show this help
  ]q                     return to curator

Examples:
  load"*",8,1           sent to Commodore, plus Enter
  ]mount /blank.d64 a    local API mount command
  ]reset                 local API reset command
""")


def drive_arg(value):
    v = (value or "a").lower()
    if v in ("8", "a"):
        return "a"
    if v in ("9", "b"):
        return "b"
    return v


def effective_keyboard_mode(host, mode):
    if mode != "auto":
        return mode
    try:
        detected = detect_machine_mode(host).get("mode", "unknown")
        if detected in ("c64", "c128"):
            return detected
    except Exception as e:
        print(f"auto mode detect failed: {e}; using c64")
    return "c64"


def run_console_command(host, command, mode):
    import shlex
    try:
        parts = shlex.split(command)
    except ValueError as e:
        print(f"parse error: {e}")
        return mode
    if not parts:
        return mode
    cmd = parts[0].lower()
    if cmd in ("?", "help"):
        command_console_help()
    elif cmd in ("q", "quit", "exit"):
        return None
    elif cmd == "mode":
        if len(parts) > 1 and parts[1].lower() in ("auto", "detect"):
            mode = "auto"
        elif len(parts) > 1 and parts[1].lower() in ("c64", "64"):
            mode = "c64"
        elif len(parts) > 1 and parts[1].lower() in ("c128", "128"):
            mode = "c128"
        print(f"mode set to {mode}")
    elif cmd == "status":
        print_drive_status(host)
    elif cmd == "reset":
        status, body = reset_machine(host)
        print(f"reset HTTP {status}: {body.strip()}")
    elif cmd == "reboot":
        status, body = reboot_machine(host)
        print(f"reboot HTTP {status}: {body.strip()}")
    elif cmd in ("unmount", "remove", "eject"):
        drive = drive_arg(parts[1] if len(parts) > 1 else "a")
        status, body = unmount_image(host, drive)
        print(f"unmount drive {drive} HTTP {status}: {body.strip()}")
    elif cmd == "mount":
        if len(parts) < 2:
            print("usage: ]mount <path> [a|b]")
        else:
            path = parts[1]
            drive = drive_arg(parts[2] if len(parts) > 2 else "a")
            status, body = mount_image(host, path, drive)
            print(f"mount {path} drive {drive} HTTP {status}: {body.strip()}")
    else:
        print(f"unknown local command: {cmd}; use ]? for help")
    return mode


def command_console(host, row):
    mode = "auto"
    print("\n" + "=" * 72)
    print("U2 keyboard command console")
    initial_mode = effective_keyboard_mode(host, mode)
    print(f"Target keyboard buffer mode: {mode} (currently detects as {initial_mode})")
    print("Type a command and press Enter to send it to the machine.")
    print("Commands are sent in <=10 character chunks and each line gets Enter appended.")
    print("Local commands start with ] or ~. Type ]? for help.")
    print()
    print_drive_status(host)
    print()
    while True:
        try:
            prompt_mode = effective_keyboard_mode(host, mode) if mode == "auto" else mode
            line = input(f"u2-{mode}/{prompt_mode}> ")
        except (EOFError, KeyboardInterrupt):
            print("\nReturning to curator...")
            return
        stripped = line.strip()
        if stripped.startswith("]") or stripped.startswith("~"):
            mode = run_console_command(host, stripped[1:].strip(), mode)
            if mode is None:
                return
            continue
        send_mode = effective_keyboard_mode(host, mode)
        send_script_keys(host, line + "\r", send_mode, chunk_size=10, chunk_delay=0.2)


def nfc_reader_device():
    preferred = Path("/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    fallback = Path("/dev/ttyUSB0")
    if preferred.exists():
        return str(preferred)
    if fallback.exists():
        return str(fallback)
    return ""


def ensure_nfc_reader_connected(verbose=False, force_attach=False):
    dev = nfc_reader_device()
    if dev and not force_attach:
        return True, dev
    script = Path(__file__).with_name("scripts") / "attach-nfc-wsl.sh"
    if not script.exists():
        return False, "NFC reader not visible and attach script not found"
    try:
        p = subprocess.run([str(script)], text=True, capture_output=True, timeout=30)
    except Exception as e:
        return False, f"NFC attach script failed: {e}"
    dev = nfc_reader_device()
    if dev:
        return True, dev
    detail = (p.stderr or p.stdout or "reader still not visible").strip().splitlines()
    return False, detail[-1] if detail else "NFC reader still not visible"


def write_nfc_for_row(row):
    ok, info = ensure_nfc_reader_connected()
    if not ok:
        print(f"NFC reader unavailable: {info}")
        return False
    payload = nfc_payload_for_row(row)
    title = row.get("title", "")
    writer = Path(__file__).with_name("nfc_write_text.py")
    cmd = [sys.executable, str(writer), "--yes", payload]

    print("\n" + "=" * 72)
    print(f"Write NFC card for: {title}")
    print(f"Payload: {payload}")
    print("Place the labeled NFC card on the reader.")
    print("WAIT FOR FULL WRITE AND VERIFY before removing the card.")
    print("If write/verify fails, this will retry automatically.")
    print("Press Ctrl-C to quit back to the curator.")

    while True:
        try:
            rc = subprocess.call(cmd)
        except KeyboardInterrupt:
            print("\nNFC write cancelled. Returning to curator...")
            return False
        if rc == 0:
            print("NFC write/verify OK. Returning to curator...")
            try:
                time.sleep(1.0)
            except KeyboardInterrupt:
                print("\nNFC write cancelled. Returning to curator...")
                return False
            return True
        print("NFC write/verify FAILED. Rechecking/reattaching reader, then retrying...")
        ok, info = ensure_nfc_reader_connected(force_attach=True)
        if not ok:
            print(f"NFC reader unavailable after reattach attempt: {info}")
            return False
        print("Place/hold the card on the reader...")
        try:
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nNFC write cancelled. Returning to curator...")
            return False


ANSI_PREVIEW_FG = {
    0: "\033[30m", 1: "\033[97m", 2: "\033[31m", 3: "\033[36m",
    4: "\033[35m", 5: "\033[32m", 6: "\033[34m", 7: "\033[33m",
    8: "\033[38;5;208m", 9: "\033[38;5;94m", 10: "\033[91m",
    11: "\033[38;5;238m", 12: "\033[38;5;244m", 13: "\033[92m", 14: "\033[94m", 15: "\033[38;5;250m",
}
ANSI_RESET = "\033[0m"


def show_prehelp_preview(script_path, image_path, machine_mode):
    _path, text = prehelp_text_for_image(image_path)
    if not text:
        return True
    # Match real renderer: force lowercase, normal upper/graphics charset.
    text = text.lower()
    bg_color = 12 if str(machine_mode).lower() in ("c128", "128") else 6
    screen_buf, color_buf, char_buf, hide_buf, _hidden, replacements = prehelp_cells(
        text, hidden_color=bg_color, lowercase_charset=False
    )
    bg = "\033[48;5;240m" if bg_color == 12 else "\033[44m"
    border = "\033[92m" if bg_color == 12 else "\033[94m"
    print("\nPrehelp preview (40x25 approximation):")
    if replacements:
        sample = "".join(dict.fromkeys(replacements[:20]))
        print(f"Warning: unsupported characters become spaces: {sample!r}")
    side = "██"
    top = "█" * (40 + len(side) * 2)
    print(border + top + ANSI_RESET)
    for y in range(25):
        line = [border + side + ANSI_RESET + bg]
        cur = None
        for x in range(40):
            off = y * 40 + x
            c = bg_color if hide_buf[off] else color_buf[off]
            if c != cur:
                line.append(ANSI_PREVIEW_FG.get(c, "\033[97m"))
                cur = c
            ch = char_buf[off]
            if ch == " ":
                line.append(" ")
            elif hide_buf[off]:
                line.append(" ")
            else:
                line.append(ch.upper())
        line.append(ANSI_RESET + border + side + ANSI_RESET)
        print("".join(line))
    print(border + top + ANSI_RESET)
    print("Accept preview? [Y/n] ", end="", flush=True)
    ans = input().strip().lower()
    return ans in ("", "y", "yes")


def prompt_script_lint_retry(script_path, problems):
    """Show lint problems outside curses. Return True to reopen editor."""
    print("\nPrehelp/script lint found possible problems:\n")
    for p in problems[:30]:
        if p.startswith("[") and p.endswith("]"):
            print(f"\n{p}")
        else:
            print(f"  - {p}")
    if len(problems) > 30:
        print(f"  ... and {len(problems) - 30} more")
    print("\nFix now? [Y/n] ", end="", flush=True)
    ans = input().strip().lower()
    return ans in ("", "y", "yes")


def lint_problem_count(problems):
    return sum(1 for p in problems if not (p.startswith("[") and p.endswith("]")))


def edit_script_with_lint(script_path, image_path="", machine_mode="c64"):
    editor = os.environ.get("EDITOR", "nano")
    strip_linter_annotations(script_path)
    while True:
        subprocess.call([editor, str(script_path)])
        # Remove prior inline guidance before checking the real script. This also
        # means answering N after an annotated edit leaves the user's content but
        # removes our temporary '# Linter:' notes.
        strip_linter_annotations(script_path)
        problems = lint_script_file(script_path)
        if problems:
            if prompt_script_lint_retry(script_path, problems):
                annotate_linter_problems(script_path, problems)
                continue
            strip_linter_annotations(script_path)
            return False, f"Script lint warnings: {lint_problem_count(problems)} issue(s)"
        if image_path and not show_prehelp_preview(script_path, image_path, machine_mode):
            continue
        return True, "OK"


DIR_ENTRY_RE = re.compile(r'^\s*(\d+)\s+"([^"]*)"\s+(del|seq|prg|usr|rel)\b', re.I)


def c1541_directory_for_row(host, row):
    """Return (raw_listing, entries) for selected disk image using c1541.

    Normal curator rows live on the U2 and must be downloaded first. A64 inbox
    rows are local candidates, so list the local file directly instead of
    round-tripping through the USB key.
    """
    path = row.get("path", "")
    lower = path.lower()
    if not lower.endswith((".d64", ".d71", ".d81")):
        raise RuntimeError("Selected item is not a supported disk image")
    suffix = Path(path).suffix or ".d64"
    tmp_path = None
    if is_local_a64_row(row):
        tmp_path = str(Path(path))
    else:
        with contextlib.redirect_stdout(io.StringIO()):
            data = ftp_download(host, path)
        with tempfile.NamedTemporaryFile(prefix="u2_dir_", suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
    try:
        p = subprocess.run(["c1541", tmp_path, "-list"], text=True, capture_output=True)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "c1541 failed").strip())
        raw = p.stdout
    finally:
        if not is_local_a64_row(row):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    entries = []
    for line in raw.splitlines():
        m = DIR_ENTRY_RE.match(line)
        if m:
            entries.append({"blocks": int(m.group(1)), "name": m.group(2), "type": m.group(3).upper(), "line": line})
    return raw, entries


def show_disk_directory(stdscr, host, row, allow_select=False):
    try:
        raw, entries = c1541_directory_for_row(host, row)
    except Exception as e:
        lines = [f"Directory unavailable: {e}", "", "Press any key to return."]
        entries = []
        allow_select = False
    else:
        lines = [f"Directory: {row.get('path','')}", ""] + raw.splitlines() + ["", "q/Esc/Enter returns."]

    if allow_select:
        pos = next((i for i, e in enumerate(entries) if e.get("type") == "PRG"), 0)
        top = 0
        msg = ""
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            body_h = max(1, h - 4)
            if entries:
                if pos < top:
                    top = pos
                if pos >= top + body_h:
                    top = pos - body_h + 1
            max_top = max(0, len(entries) - body_h)
            top = max(0, min(top, max_top))

            stdscr.addnstr(0, 0, f"Directory: {row.get('path','')}", w-1, curses.A_BOLD)
            stdscr.addnstr(1, 0, "blocks  type  name", w-1, curses.A_DIM)
            for y, idx in enumerate(range(top, min(len(entries), top + body_h)), start=2):
                e = entries[idx]
                line = f"{e['blocks']:>5}  {e['type']:<3}   {e['name']}"
                attr = curses.A_REVERSE if idx == pos else 0
                if e.get("type") != "PRG":
                    attr |= curses.A_DIM
                stdscr.addnstr(y, 0, line, w-1, attr)
            if msg:
                stdscr.addnstr(h-2, 0, msg, w-1, curses.color_pair(3) if curses.has_colors() else 0)
            footer = "↑/↓ move  PgUp/PgDn page  Home/End  Enter select PRG  q/Esc cancel"
            if entries:
                footer += f"  {pos+1}/{len(entries)}"
            stdscr.addnstr(h-1, 0, footer, w-1, curses.A_DIM)
            stdscr.refresh()
            ch = stdscr.getch()
            msg = ""
            if ch in (ord('q'), 27):
                return None
            if ch in (10, 13):
                if entries and entries[pos].get("type") == "PRG":
                    return entries[pos]["name"]
                msg = "Selected directory entry is not a PRG"
            elif ch == curses.KEY_UP:
                pos = max(0, pos - 1)
            elif ch == curses.KEY_DOWN:
                pos = min(len(entries) - 1, pos + 1) if entries else 0
            elif ch == curses.KEY_PPAGE:
                pos = max(0, pos - max(1, body_h - 1))
            elif ch == curses.KEY_NPAGE:
                pos = min(len(entries) - 1, pos + max(1, body_h - 1)) if entries else 0
            elif ch == curses.KEY_HOME:
                pos = 0
            elif ch == curses.KEY_END:
                pos = len(entries) - 1 if entries else 0
        
    top = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 1)
        max_top = max(0, len(lines) - body_h)
        top = max(0, min(top, max_top))
        for y, line in enumerate(lines[top:top + body_h]):
            abs_i = top + y
            attr = curses.A_BOLD if abs_i == 0 else 0
            stdscr.addnstr(y, 0, line, w-1, attr)
        footer = "↑/↓ scroll  PgUp/PgDn page  Home/End  q/Esc/Enter return"
        if len(lines) > body_h:
            footer += f"  {top+1}-{min(len(lines), top+body_h)}/{len(lines)}"
        stdscr.addnstr(h-1, 0, footer, w-1, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), 27, 10, 13, ord(' ')):
            return None
        if ch == curses.KEY_UP:
            top -= 1
        elif ch == curses.KEY_DOWN:
            top += 1
        elif ch == curses.KEY_PPAGE:
            top -= body_h
        elif ch == curses.KEY_NPAGE:
            top += body_h
        elif ch == curses.KEY_HOME:
            top = 0
        elif ch == curses.KEY_END:
            top = max_top


def prompt_line(stdscr, prompt, default="", max_len=120):
    h, w = stdscr.getmaxyx()
    curses.echo(); curses.curs_set(1)
    stdscr.move(h-1, 0); stdscr.clrtoeol()
    full = f"{prompt} [{default}]: " if default else f"{prompt}: "
    stdscr.addnstr(h-1, 0, full, w-1)
    stdscr.refresh()
    text = stdscr.getstr(h-1, min(len(full), w-2), max_len).decode(errors="replace").strip()
    curses.noecho(); curses.curs_set(0)
    return text if text else default


def a64_pick_value(current, values):
    if not values:
        return current
    try:
        idx = values.index(current)
        return values[(idx + 1) % len(values)]
    except ValueError:
        return values[0]


def popup_multi_select(stdscr, title, values, selected_csv=""):
    values = [v for v in values if v]
    selected = {v.strip() for v in str(selected_csv or "").split(",") if v.strip()}
    pos = 0
    top = 0
    while True:
        h, w = stdscr.getmaxyx()
        width = min(w - 4, max(44, len(title) + 4, *(len(v) + 8 for v in values)) if values else 44)
        height = min(h - 2, max(6, min(len(values) + 4, h - 2)))
        body_h = max(1, height - 4)
        if pos < top:
            top = pos
        if pos >= top + body_h:
            top = pos - body_h + 1
        top = max(0, min(top, max(0, len(values) - body_h)))
        y0 = max(0, (h - height) // 2)
        x0 = max(0, (w - width) // 2)
        win = curses.newwin(height, width, y0, x0)
        win.keypad(True)
        win.box()
        win.addnstr(1, 2, title, width - 4, curses.A_BOLD)
        if not values:
            win.addnstr(2, 2, "No options available", width - 4, curses.A_DIM)
        for row_y, idx in enumerate(range(top, min(len(values), top + body_h)), start=2):
            v = values[idx]
            mark = "[x]" if v in selected else "[ ]"
            attr = curses.A_REVERSE if idx == pos else 0
            win.addnstr(row_y, 2, f"{mark} {v}", width - 4, attr)
        footer = "Space toggle  Enter OK  c clear  q cancel"
        win.addnstr(height - 1, 2, footer, width - 4, curses.A_DIM)
        win.refresh()
        ch = win.getch()
        if ch in (ord('q'), ord('Q'), 27):
            return None
        if ch == curses.KEY_UP:
            pos = max(0, pos - 1)
        elif ch == curses.KEY_DOWN:
            pos = min(len(values) - 1, pos + 1)
        elif ch == curses.KEY_PPAGE:
            pos = max(0, pos - body_h)
        elif ch == curses.KEY_NPAGE:
            pos = min(len(values) - 1, pos + body_h)
        elif ch == curses.KEY_HOME:
            pos = 0
        elif ch == curses.KEY_END:
            pos = max(0, len(values) - 1)
        elif ch == ord('c'):
            selected.clear()
        elif ch == ord(' ') and values:
            v = values[pos]
            if v in selected:
                selected.remove(v)
            else:
                selected.add(v)
        elif ch in (10, 13):
            return ",".join(v for v in values if v in selected)


def a64_search_form(stdscr, defaults=None):
    defaults = defaults or {}
    fields = [
        {"key": "name", "label": "Name", "value": "", "kind": "text", "help": "title/name search text"},
        {"key": "types", "label": "Type(s)", "value": "", "kind": "combo", "values": ["d64", "d71", "d81", "prg", "crt", "sid"], "multi": True},
        {"key": "category", "label": "Category", "value": "", "kind": "combo", "values": ["", "games", "c128", "easyflash", "tools", "demos", "music", "misc"], "multi": True},
        {"key": "subcat", "label": "Subcategory", "value": "", "kind": "combo", "values": ["", "oneload64", "gamebase", "games", "c128stuff", "presdisk", "prestap", "guybrushgames", "c64comgames"], "multi": True},
        {"key": "repo", "label": "Repo", "value": "", "kind": "combo", "values": ["", "oneload", "gamebase", "csdb", "c64com", "guybrush", "utape", "tapes"], "multi": True},
        {"key": "group", "label": "Group", "value": "", "kind": "text", "help": "release/crack group, optional"},
        {"key": "handle", "label": "Handle", "value": "", "kind": "text", "help": "person/handle, optional"},
        {"key": "sort", "label": "Sort", "value": "", "kind": "combo", "values": ["", "name", "group", "handle", "event", "year", "rating"]},
        {"key": "order", "label": "Order", "value": "", "kind": "combo", "values": ["", "asc", "desc"]},
        {"key": "latest", "label": "Latest", "value": "", "kind": "combo", "values": ["", "1week", "1month", "3months", "6months", "1year", "2years"]},
    ]
    for f in fields:
        if f["key"] in defaults:
            f["value"] = defaults.get(f["key"], "")
    pos = 0
    top = 0
    msg = "Enter edits text; Space cycles combo; s searches; Shift-S searches even with blank/short name; q cancels"
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 4)
        if pos < top:
            top = pos
        if pos >= top + body_h:
            top = pos - body_h + 1
        stdscr.addnstr(0, 0, "Assembly64 search criteria", w-1, curses.A_BOLD)
        stdscr.addnstr(1, 0, "Edit fields, then press s to search.", w-1, curses.A_DIM)
        for y, idx in enumerate(range(top, min(len(fields), top + body_h)), start=2):
            f = fields[idx]
            kind = "combo" if f.get("kind") == "combo" else "text"
            extra = " multi" if f.get("multi") else ""
            line = f"{f['label']:<12} {f['value'] or '<any>'}  ({kind}{extra})"
            attr = curses.A_REVERSE if idx == pos else 0
            stdscr.addnstr(y, 0, line, w-1, attr)
        stdscr.addnstr(h-2, 0, msg, w-1)
        footer = "↑/↓ move  Enter edit  Space cycle combo  c clear  s search  Shift-S override  q/Esc cancel"
        stdscr.addnstr(h-1, 0, footer, w-1, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), 27):
            return None
        if ch == curses.KEY_UP:
            pos = max(0, pos - 1)
        elif ch == curses.KEY_DOWN:
            pos = min(len(fields) - 1, pos + 1)
        elif ch == curses.KEY_HOME:
            pos = 0
        elif ch == curses.KEY_END:
            pos = len(fields) - 1
        elif ch == ord('c'):
            fields[pos]["value"] = ""
        elif ch == ord(' ') and fields[pos].get("kind") == "combo":
            fields[pos]["value"] = a64_pick_value(fields[pos].get("value", ""), fields[pos].get("values", []))
        elif ch in (10, 13):
            f = fields[pos]
            if f.get("kind") == "combo":
                picked = popup_multi_select(stdscr, f"A64 {f['label']}", f.get("values", []), f.get("value", ""))
                if picked is not None:
                    f["value"] = picked
            else:
                f["value"] = prompt_line(stdscr, f"A64 {f['label']}", f.get("value", ""), 160)
        elif ch in (ord('s'), ord('S')):
            data = {f["key"]: normalize_a64_search_value(f.get("value", "")) for f in fields}
            if ch == ord('s') and len(data.get("name", "")) < 3:
                msg = "Name must be at least 3 characters; use Shift-S to override"
                continue
            return data


def is_a64_row(row):
    return bool(row.get("a64_id") and row.get("a64_category") is not None and row.get("entry_index") is not None)


def is_local_a64_row(row):
    p = str(row.get("path") or "")
    return is_a64_row(row) and p and not p.startswith("/") and Path(p).exists()


def normalize_a64_search_value(value):
    return str(value or "").strip().lower()


def update_a64_entry_state(db_path, row, state):
    if not is_a64_row(row):
        return False
    col = {
        "failed": "failed_at",
        "deleted": "deleted_at",
        "discarded": "discarded_at",
        "promoted": "promoted_at",
        "tested": "tested_at",
    }.get(state)
    if not col:
        return False
    with a64_db_connect(db_path) as conn:
        conn.execute(
            f"""
            UPDATE A64Entry
            SET {col}=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE pk_ID IN (
              SELECT e.pk_ID FROM A64Entry e
              JOIN A64Result r ON r.pk_ID=e.fk_A64Result_ID
              WHERE r.a64_id=? AND r.a64_category=? AND e.entry_index=?
            )
            """,
            (str(row.get("a64_id")), int(row.get("a64_category") or 0), int(row.get("entry_index") or 0)),
        )
    row["status"] = state
    row["a64_status"] = state
    return True


def safe_filename_title(title):
    s = re.sub(r"[^A-Za-z0-9 _.-]+", "_", str(title or "")).strip(" ._")
    s = re.sub(r"\s+", "_", s)
    return s or "A64_Image"


def a64_bucket(title):
    s = safe_filename_title(title)
    ch = s[0].upper() if s else "#"
    return ch if "A" <= ch <= "Z" else "#"


def suggested_a64_promote_path(row):
    title = row.get("title") or Path(row.get("original_filename") or row.get("path", "A64_Image")).stem
    safe_title = safe_filename_title(title)
    a64_id = str(row.get("a64_id") or "a64")
    short_id = a64_id if len(a64_id) <= 12 else a64_id[:12]
    ext = Path(row.get("path") or row.get("original_filename") or "").suffix.lower() or ".bin"
    return f"/Usb0/A64/{a64_bucket(safe_title)}/{safe_title}-{short_id}{ext}"


def mode_for_ext(ext):
    ext = ext.lower().lstrip(".")
    if ext in ("d64", "d71", "d81", "g64"):
        return "disk"
    if ext == "crt":
        return "crt"
    if ext == "sid":
        return "sid"
    if ext == "tap":
        return "tap"
    return "prg"


def mark_image_deleted(db_path, image_path, reason="Deleted from TUI"):
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        conn.execute(
            """
            UPDATE Image
            SET fk_StorageStatus_ID=?, deleted_reason=?, deleted_at=CURRENT_TIMESTAMP
            WHERE path=?
            """,
            (lookup_id(conn, "StorageStatus", "deleted"), reason, image_path),
        )


def delete_curator_image(stdscr, host, db_path, row, base_rows):
    path = row.get("path", "")
    title = row.get("title", "") or path
    if not path:
        raise RuntimeError("Selected row has no path")
    detail = f"Delete curated image: {title}\n\nU2 path: {path}\n\nThis removes the file from the U2 and marks the DB row deleted."
    if not confirm_action(stdscr, "Delete curated image", detail):
        return False, "Delete cancelled"
    try:
        ftp_delete(host, path)
    except Exception:
        # If the file is already gone, still mark the DB deleted.
        pass
    mark_image_deleted(db_path, path)
    for i, r in enumerate(list(base_rows)):
        if r.get("path") == path:
            del base_rows[i]
            break
    return True, f"Deleted curated image: {title}"


def a64_record_local_candidate(db_path, local_path, title, source_row, file_type=None):
    p = Path(local_path)
    ext = (file_type or p.suffix.lower().lstrip(".") or "other")
    source_id = str(source_row.get("a64_id") or "local")
    result = {
        "id": f"{source_id}-local-{p.stem}",
        "category": int(source_row.get("a64_category") or 0),
        "name": title,
        "group": source_row.get("group_name", ""),
        "year": int(source_row.get("year") or 0),
    }
    entry = type("Entry", (), {
        "entry_index": 0,
        "path": p.name,
        "suffix": ext,
        "size": p.stat().st_size if p.exists() else None,
        "date": None,
        "raw": {"path": p.name, "id": 0, "size": p.stat().st_size if p.exists() else None, "derived_from": source_id},
    })()
    with a64_db_connect(db_path) as conn:
        result_pk = a64_upsert_result(conn, result)
        entry_pk = a64_upsert_entry(conn, result_pk, entry, str(p))
        conn.execute("UPDATE A64Entry SET title=?, deleted_at=NULL, discarded_at=NULL, failed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE pk_ID=?", (title, entry_pk))


def create_local_prg_image(local_prg, target_ext, title, output_dir=DEFAULT_INBOX):
    if not c1541_available():
        raise RuntimeError("c1541 is not installed")
    local_prg = Path(local_prg)
    out = Path(output_dir) / f"{local_prg.stem}.converted.{target_ext}"
    tmp = tempfile.NamedTemporaryFile(suffix=f".{target_ext}", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    disk_type = target_ext.lower()
    disk_title = safe_filename_title(title)[:16] or "a64"
    try:
        r = subprocess.run(["c1541", "-format", f"{disk_title},64", disk_type, tmp.name, "-write", str(local_prg), local_prg.stem[:16]], text=True, capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "c1541 conversion failed").strip())
        shutil.move(tmp.name, out)
        return out
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass


def convert_local_disk_image(local_disk, target_ext, title, output_dir=DEFAULT_INBOX):
    if not c1541_available():
        raise RuntimeError("c1541 is not installed")
    local_disk = Path(local_disk)
    src_info = c1541_directory_from_file(local_disk)
    out = Path(output_dir) / f"{local_disk.stem}.converted.{target_ext}"
    tmpdir = Path(tempfile.mkdtemp(prefix="a64_convert_"))
    tmpout = tempfile.NamedTemporaryFile(suffix=f".{target_ext}", delete=False)
    tmpout.close(); os.unlink(tmpout.name)
    try:
        disk_title = safe_filename_title(src_info.get("disk_name") or title)[:16] or "a64"
        r = subprocess.run(["c1541", "-format", f"{disk_title},64", target_ext, tmpout.name], text=True, capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "c1541 format failed").strip())
        for e in src_info.get("entries", []):
            if e.get("type") != "PRG":
                continue
            name = e.get("name", "")
            host_file = tmpdir / re.sub(r"[^A-Za-z0-9._-]+", "_", name or "entry.prg")
            r = subprocess.run(["c1541", str(local_disk), "-read", name, str(host_file)], text=True, capture_output=True, timeout=60)
            if r.returncode != 0 or not host_file.exists():
                continue
            r = subprocess.run(["c1541", tmpout.name, "-write", str(host_file), name], text=True, capture_output=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or f"c1541 write failed for {name}").strip())
        shutil.move(tmpout.name, out)
        return out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            os.unlink(tmpout.name)
        except FileNotFoundError:
            pass


def choose_menu(stdscr, title, items):
    """Popup menu used by image actions. Return selected item dict, or None."""
    enabled_positions = [i for i, item in enumerate(items) if item.get("enabled", True)]
    if not enabled_positions:
        return None
    pos = enabled_positions[0]
    top = 0
    while True:
        h, w = stdscr.getmaxyx()
        width = min(w - 4, max(48, len(title) + 4, *(len(it.get("label", "")) + len(it.get("hint", "")) + 8 for it in items)))
        height = min(h - 2, len(items) + 4)
        body_h = max(1, height - 3)
        if pos < top:
            top = pos
        if pos >= top + body_h:
            top = pos - body_h + 1
        top = max(0, min(top, max(0, len(items) - body_h)))
        y0 = max(0, (h - height) // 2)
        x0 = max(0, (w - width) // 2)
        win = curses.newwin(height, width, y0, x0)
        win.keypad(True)
        win.box()
        win.addnstr(1, 2, title, width - 4, curses.A_BOLD)
        for row_y, item_idx in enumerate(range(top, min(len(items), top + body_h)), start=2):
            item = items[item_idx]
            hint = f"  {item.get('hint')}" if item.get("hint") else ""
            line = f"{item.get('label','')}{hint}"
            attr = curses.A_REVERSE if item_idx == pos else 0
            if not item.get("enabled", True):
                attr |= curses.A_DIM
            win.addnstr(row_y, 2, line, width - 4, attr)
        if len(items) > body_h:
            win.addnstr(height - 1, 2, f"{top+1}-{min(len(items), top+body_h)}/{len(items)}", width - 4, curses.A_DIM)
        win.refresh()
        ch = win.getch()
        if ch in (ord('q'), ord('Q'), 27):
            return None
        if ch == curses.KEY_UP:
            candidates = [i for i in enabled_positions if i < pos]
            pos = candidates[-1] if candidates else enabled_positions[-1]
        elif ch == curses.KEY_DOWN:
            candidates = [i for i in enabled_positions if i > pos]
            pos = candidates[0] if candidates else enabled_positions[0]
        elif ch == curses.KEY_PPAGE:
            pos = enabled_positions[max(0, enabled_positions.index(pos) - body_h)]
        elif ch == curses.KEY_NPAGE:
            pos = enabled_positions[min(len(enabled_positions) - 1, enabled_positions.index(pos) + body_h)]
        elif ch in (10, 13):
            return items[pos] if items[pos].get("enabled", True) else None


def conversion_targets(file_type):
    return {
        "prg": ["d64", "d71", "d81"],
        "d64": ["d71", "d81"],
        "d71": ["d81"],
    }.get(str(file_type).lower().lstrip("."), [])


def image_action_items_for_row(row, *, backend="u2"):
    ft = (row.get("file_type") or row.get("type") or Path(row.get("path", "")).suffix.lstrip(".")).lower()
    targets = conversion_targets(ft)
    if backend == "a64":
        return [
            {"label": f"Convert {ft.upper()} to...", "action": "convert", "enabled": bool(targets)},
            {"label": f"Extract PRG from {ft.upper()}...", "action": "extract", "enabled": ft in ("d64", "d71", "d81"), "hint": "reserved for A64"},
            {"label": "Delete local candidate...", "action": "delete", "enabled": False, "hint": "use x from A64 inbox"},
            {"label": "Promote to U2...", "action": "promote", "enabled": False, "hint": "use p from A64 inbox"},
        ]
    return [
        {"label": f"Convert {ft.upper()} to...", "action": "convert", "enabled": bool(targets)},
        {"label": f"Extract PRG from {ft.upper()}...", "action": "extract", "enabled": ft in ("d64", "d71", "d81")},
        {"label": "Delete image...", "action": "delete", "enabled": True},
    ]


def run_local_conversion_action(local, row, target, output_dir=DEFAULT_INBOX):
    ft = Path(local).suffix.lower().lstrip(".")
    title = row.get("title") or Path(local).stem
    if ft == "prg":
        return create_local_prg_image(local, target, title, output_dir=output_dir)
    if ft in ("d64", "d71"):
        return convert_local_disk_image(local, target, title, output_dir=output_dir)
    raise RuntimeError(f"{ft.upper()} conversion reserved")


def normal_image_actions_menu(stdscr, host, state_path, row, base_rows):
    chosen = choose_menu(stdscr, f"Image actions: {row.get('title','')}", image_action_items_for_row(row, backend="u2"))
    if not chosen:
        return "Image actions cancelled"
    if chosen["action"] == "delete":
        ok, message = delete_curator_image(stdscr, host, state_path, row, base_rows)
        return message
    if chosen["action"] == "convert":
        return "Conversion for U2 images is not available on this branch yet"
    if chosen["action"] == "extract":
        return "Extract PRG is not available on this branch yet"
    return "Action reserved"


def a64_actions_menu(stdscr, state_path, row):
    local = Path(row.get("path", ""))
    ft = local.suffix.lower().lstrip(".")
    targets = conversion_targets(ft)
    chosen = choose_menu(stdscr, f"Image actions: {row.get('title','')}", image_action_items_for_row(row, backend="a64"))
    if not chosen:
        return "Image actions cancelled"
    if chosen["action"] == "convert":
        sub = choose_menu(stdscr, f"Convert {ft.upper()} to...", [{"label": t.upper(), "target": t} for t in targets])
        if not sub:
            return "Conversion cancelled"
        target = sub["target"]
        title = row.get("title") or local.stem
        out = run_local_conversion_action(local, row, target, output_dir=DEFAULT_INBOX)
        a64_record_local_candidate(state_path, out, f"{title} - Converted [to {target.upper()}]", row, target)
        # Conversion creates a derived local pseudo-result. It must not rewrite
        # the source A64 entry's filename/path/type in the inbox.
        if "-local-" not in str(row.get("a64_id", "")):
            with a64_db_connect(state_path) as conn:
                conn.execute(
                    """
                    UPDATE A64Entry
                    SET original_filename=?, file_type=?, local_path=?, updated_at=CURRENT_TIMESTAMP
                    WHERE pk_ID IN (
                      SELECT e.pk_ID FROM A64Entry e JOIN A64Result r ON r.pk_ID=e.fk_A64Result_ID
                      WHERE r.a64_id=? AND r.a64_category=? AND e.entry_index=?
                    )
                    """,
                    (
                        row.get("original_filename") or local.name,
                        local.suffix.lower().lstrip("."),
                        str(local),
                        str(row.get("a64_id")),
                        int(row.get("a64_category") or 0),
                        int(row.get("entry_index") or 0),
                    ),
                )
        return f"A64 converted to {out.name}"
    return "Action reserved"


def promote_a64_candidate(stdscr, host, db_path, row, base_rows):
    if not is_local_a64_row(row):
        raise RuntimeError("Selected row is not a local A64 candidate")
    local = Path(row.get("path", ""))
    if not local.exists():
        raise RuntimeError(f"Local file missing: {local}")
    default_title = row.get("title") or Path(row.get("original_filename") or local.name).stem
    title = prompt_line(stdscr, "Promoted title", default_title, 120)
    suggested = suggested_a64_promote_path({**row, "title": title})
    dest = prompt_line(stdscr, "U2 destination path", suggested, 220)
    if not dest:
        raise RuntimeError("Promotion cancelled: no destination")
    if not dest.startswith("/"):
        dest = "/Usb0/" + dest.lstrip("/")
    data = local.read_bytes()
    h, w = stdscr.getmaxyx()
    stdscr.move(h - 1, 0)
    stdscr.clrtoeol()
    attr = curses.A_BOLD | (curses.color_pair(5) if curses.has_colors() else 0)
    stdscr.addnstr(h - 1, 0, f"Uploading promoted file to U2: {dest}", w - 1, attr)
    stdscr.refresh()
    uploaded = ftp_upload(host, dest, data)
    image_path = strip_usb_prefix(uploaded)
    ext = local.suffix.lower().lstrip(".")
    mode = mode_for_ext(ext)
    notes = f"A64 id={row.get('a64_id')} category={row.get('a64_category')} entry={row.get('entry_index')} original={row.get('original_filename') or local.name}"
    payload = payload_for(mode, image_path, "")
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        vals = {
            "title": title,
            "path": image_path,
            "payload": payload,
            "entry": "",
            "detail": f"A64 promoted {ext.upper()}",
            "notes": notes,
            "quarantine_reason": "",
            "deleted_reason": "",
            "quarantined": 0,
            "fk_Status_ID": lookup_id(conn, "Status", "approved"),
            "fk_StorageStatus_ID": lookup_id(conn, "StorageStatus", "present"),
            "fk_MachineMode_ID": lookup_id(conn, "MachineMode", row.get("machine_mode") or "c64"),
            "fk_FileType_ID": lookup_id(conn, "FileType", ext),
            "fk_LaunchMode_ID": lookup_id(conn, "LaunchMode", mode),
        }
        cols = ", ".join(vals)
        placeholders = ", ".join("?" for _ in vals)
        updates = ", ".join(f"{k}=excluded.{k}" for k in vals if k != "path")
        conn.execute(
            f"INSERT INTO Image ({cols}) VALUES ({placeholders}) ON CONFLICT(path) DO UPDATE SET {updates}",
            list(vals.values()),
        )
    with a64_db_connect(db_path) as conn:
        conn.execute(
            """
            UPDATE A64Entry
            SET promoted_path=?, promoted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE pk_ID IN (
              SELECT e.pk_ID FROM A64Entry e JOIN A64Result r ON r.pk_ID=e.fk_A64Result_ID
              WHERE r.a64_id=? AND r.a64_category=? AND e.entry_index=?
            )
            """,
            (image_path, str(row.get("a64_id")), int(row.get("a64_category") or 0), int(row.get("entry_index") or 0)),
        )
    try:
        local.unlink()
    except FileNotFoundError:
        pass
    new_row = {"title": title, "path": image_path, "payload": payload, "mode": mode, "entry": "", "machine_mode": row.get("machine_mode") or "c64", "file_type": ext, "type": ext, "detail": f"A64 promoted {ext.upper()}", "status": "approved", "storage_status": "present", "notes": notes}
    existing = next((r for r in base_rows if r.get("path") == image_path), None)
    if existing:
        existing.update(new_row)
    else:
        base_rows.append(new_row)
    row["promoted_path"] = image_path
    row["promoted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    row["status"] = "promoted"
    row["a64_status"] = "promoted"
    return image_path, new_row


def clear_a64_entry_state(db_path, row):
    if not is_a64_row(row):
        return False
    with a64_db_connect(db_path) as conn:
        conn.execute(
            """
            UPDATE A64Entry
            SET failed_at=NULL, discarded_at=NULL, deleted_at=NULL, tested_at=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE pk_ID IN (
              SELECT e.pk_ID FROM A64Entry e
              JOIN A64Result r ON r.pk_ID=e.fk_A64Result_ID
              WHERE r.a64_id=? AND r.a64_category=? AND e.entry_index=?
            )
            """,
            (str(row.get("a64_id")), int(row.get("a64_category") or 0), int(row.get("entry_index") or 0)),
        )
    row["status"] = "downloaded" if row.get("downloaded_at") else "seen"
    row["a64_status"] = row["status"]
    return True


def a64_scratch_remote_candidates(row, local):
    """Candidate names for transient U2 A64 test uploads.

    U2 FTP can report a successful STOR for certain filenames but then fail to
    list/SIZE/mount them (seen with 5076_16_0_MULE.D64). Try the original cache
    name first for readability, then increasingly boring variants.
    """
    rid = str(row.get("a64_id") or "a64")
    cat = str(row.get("a64_category") or "0")
    idx = str(row.get("entry_index") or "0")
    suffix = local.suffix or ".bin"
    stem = local.stem or "entry"
    safe_stem = re.sub(r"[^A-Za-z0-9.-]+", "-", stem).strip(".-") or "entry"
    names = [
        local.name,
        f"{stem}_{suffix}",
        f"{stem}-{suffix}",
        f"{stem}_x{suffix}",
        f"x{stem}{suffix}",
        f"{stem.replace('_', '-')}{suffix}",
        f"a64-{rid}-{cat}-{idx}-{safe_stem}{suffix.lower()}",
        f"a64-{rid}-{cat}-{idx}{suffix.lower()}",
    ]
    out = []
    seen = set()
    for name in names:
        name = re.sub(r"[\\/:]+", "-", name)
        key = name.lower()
        if key not in seen:
            out.append(name)
            seen.add(key)
    return out


def upload_a64_scratch_verified(stdscr, host, row, local, data, status_callback=None):
    last_err = None
    for n, name in enumerate(a64_scratch_remote_candidates(row, local), 1):
        remote = "/_A64_Test/" + name
        if status_callback:
            status_callback(f"Uploading A64 test image {n}: {remote} (USB auto)", "work")
        else:
            h, w = stdscr.getmaxyx()
            stdscr.move(h-1, 0); stdscr.clrtoeol()
            stdscr.addnstr(h-1, 0, f"Uploading A64 test image {n}: {remote} (USB auto)", w-1)
            stdscr.refresh()
        try:
            uploaded = ftp_upload(host, remote, data)
            ftp_size(host, uploaded)
            if status_callback:
                status_callback(f"Uploaded A64 test image to U2: {uploaded}", "work")
            else:
                h, w = stdscr.getmaxyx()
                stdscr.move(h-1, 0); stdscr.clrtoeol()
                stdscr.addnstr(h-1, 0, f"Uploaded A64 test image to U2: {uploaded}", w-1)
                stdscr.refresh()
            return uploaded
        except Exception as e:
            last_err = e
            try:
                ftp_delete(host, uploaded if 'uploaded' in locals() else remote)
            except Exception:
                pass
            if status_callback:
                status_callback(f"Upload name failed; retrying: {name}", "work")
    raise RuntimeError(f"A64 scratch upload failed for all filename variants; last error: {last_err}")


def launch_local_a64_candidate(stdscr, host, row, target_mode="c64", skip_prehelp=False, status_callback=None):
    local = Path(row.get("path", ""))
    if not local.exists():
        raise RuntimeError(f"Local A64 file not found: {local}")
    ext = local.suffix.lower()
    if ext == ".prg":
        data = local.read_bytes()
        if not data:
            raise RuntimeError(f"Local PRG is 0 bytes: {local}")
        remote_prg = upload_a64_scratch_verified(stdscr, host, row, local, data, status_callback=status_callback)
        if status_callback:
            status_callback("Preparing PRG save sidecar D81", "work")
        sidecar = ensure_prg_sidecar_d81(host, remote_prg)
        if status_callback:
            status_callback(f"Mounting PRG save sidecar {sidecar}", "work")
        mount_image(host, sidecar, "a")
        time.sleep(0.5)
        if status_callback:
            status_callback("Launching PRG", "normal")
        result = post_runner(host, "/v1/runners:run_prg", data)
        return result
    if ext == ".crt":
        data = local.read_bytes()
        if not data:
            raise RuntimeError(f"Local CRT is 0 bytes: {local}")
        remote_crt = upload_a64_scratch_verified(stdscr, host, row, local, data, status_callback=status_callback)
        if status_callback:
            status_callback("Preparing CRT save sidecar D81", "work")
        sidecar = ensure_crt_sidecar_d81(host, remote_crt)
        if status_callback:
            status_callback(f"Mounting CRT save sidecar {sidecar}", "work")
        mount_image(host, sidecar, "a")
        time.sleep(0.5)
        if status_callback:
            status_callback("Launching CRT", "normal")
        return post_runner(host, "/v1/runners:run_crt", data)
    if ext == ".sid":
        data = local.read_bytes()
        if not data:
            raise RuntimeError(f"Local SID is 0 bytes: {local}")
        if status_callback:
            status_callback("Launching SID player")
        return post_runner(host, "/v1/runners:sidplay", data)
    if ext in (".d64", ".d71", ".d81"):
        uploaded = upload_a64_scratch_verified(stdscr, host, row, local, local.read_bytes(), status_callback=status_callback)
        # For scratch/test uploads, keep the concrete /UsbX path. NFC payloads
        # intentionally strip /UsbX for portability, but immediate A64 tests
        # should mount the exact file we just uploaded and not depend on the
        # last-known USB fallback state.
        time.sleep(0.5)
        payload = f"U2+:disk:{uploaded}" + (f"#{row.get('entry', '')}" if row.get('entry', '') else "")
        return launch_payload(host, payload, target_mode=target_mode, skip_prehelp=skip_prehelp, status_callback=status_callback)
    raise RuntimeError(f"Unsupported local A64 file type: {ext}")


def a64_result_payload_map(state_path, results):
    keys = [(str(r.get("id", "")), int(r.get("category") or 0)) for r in results]
    out = {}
    if not keys:
        return out
    try:
        placeholders = ",".join(["(?,?)"] * len(keys))
        params = []
        for rid, cat in keys:
            params.extend([rid, cat])
        with a64_db_connect(state_path) as conn:
            for row in conn.execute(
                f"""
                WITH wanted(a64_id, a64_category) AS (VALUES {placeholders})
                SELECT r.a64_id, r.a64_category, r.entries_count, r.entries_http_status
                FROM wanted w
                JOIN A64Result r ON r.a64_id = w.a64_id AND r.a64_category = w.a64_category
                WHERE r.entries_checked_at IS NOT NULL
                """,
                params,
            ):
                count = int(row[2] or 0)
                http_status = row[3]
                out[(str(row[0]), int(row[1]))] = {"count": count, "http_status": http_status}
    except Exception:
        pass
    return out


def a64_result_marker_map(state_path, results):
    """Return {(a64_id, category): marker} for a visible A64 result page.

    Keep redraw/navigation cheap: one SQLite query per page, not one query per
    visible row on every keypress.
    """
    keys = [(str(r.get("id", "")), int(r.get("category") or 0)) for r in results]
    if not keys:
        return {}
    out = {k: " " for k in keys}
    try:
        placeholders = ",".join(["(?,?)"] * len(keys))
        params = []
        for rid, cat in keys:
            params.extend([rid, cat])
        with a64_db_connect(state_path) as conn:
            for row in conn.execute(
                f"""
                WITH wanted(a64_id, a64_category) AS (VALUES {placeholders})
                SELECT r.a64_id, r.a64_category,
                  SUM(CASE WHEN e.promoted_at IS NOT NULL OR i.pk_ID IS NOT NULL THEN 1 ELSE 0 END) AS promoted,
                  SUM(CASE WHEN e.tested_at IS NOT NULL THEN 1 ELSE 0 END) AS tested,
                  SUM(CASE WHEN e.downloaded_at IS NOT NULL THEN 1 ELSE 0 END) AS downloaded,
                  GROUP_CONCAT(CASE WHEN e.downloaded_at IS NOT NULL THEN e.local_path ELSE NULL END, CHAR(10)) AS downloaded_paths
                FROM wanted w
                JOIN A64Result r ON r.a64_id = w.a64_id AND r.a64_category = w.a64_category
                LEFT JOIN A64Entry e ON e.fk_A64Result_ID = r.pk_ID
                LEFT JOIN Image i ON i.path = e.promoted_path AND COALESCE(e.promoted_path, '') != ''
                GROUP BY r.a64_id, r.a64_category
                """,
                params,
            ):
                rid = str(row[0]); cat = int(row[1])
                promoted = int(row[2] or 0)
                tested = int(row[3] or 0)
                downloaded = int(row[4] or 0)
                downloaded_paths = [p for p in str(row[5] or "").split("\n") if p]
                downloaded_local = any(Path(p).exists() for p in downloaded_paths)
                out[(rid, cat)] = "💾" if promoted else ("👓" if tested else ("☎" if downloaded_local else ("↓" if downloaded else " ")))
    except Exception:
        pass
    return out


def a64_inbox_files():
    root = DEFAULT_INBOX
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_file())


def reconcile_a64_inbox(state_path):
    files = a64_inbox_files()
    pat = re.compile(r"^(.+?)_(\d+)_(\d+)_(.+)$")
    with a64_db_connect(state_path) as conn:
        for p in files:
            conn.execute(
                "UPDATE A64Entry SET deleted_at=NULL, discarded_at=NULL, failed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE local_path=?",
                (str(p),),
            )
            if ".converted." in p.name:
                # Converted files are recorded as derived local pseudo-results by
                # a64_record_local_candidate(). Do not parse their source-like
                # prefix (e.g. 5076_16_0_MULE.converted.d71) as the original
                # A64 identity and overwrite the source entry.
                continue
            m = pat.match(p.name)
            if not m:
                continue
            rid, cat, idx, original = m.groups()
            result = {"id": rid, "category": int(cat), "name": Path(original).stem}
            result_pk = a64_upsert_result(conn, result)
            entry = type("Entry", (), {
                "entry_index": int(idx),
                "path": original,
                "suffix": Path(original).suffix.lower().lstrip(".") or "other",
                "size": p.stat().st_size,
                "date": None,
                "raw": {"path": original, "id": int(idx), "size": p.stat().st_size},
            })()
            a64_upsert_entry(conn, result_pk, entry, str(p))
    return files


def clear_a64_inbox(state_path):
    files = a64_inbox_files()
    paths = [str(p) for p in files]
    for p in files:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    if paths:
        with a64_db_connect(state_path) as conn:
            conn.executemany(
                "UPDATE A64Entry SET deleted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE local_path=?",
                [(p,) for p in paths],
            )
    return len(paths)


def prompt_yes_no(stdscr, title, detail, default_no=True):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    lines = [title, ""] + str(detail).splitlines() + ["", "Answer [y/N]:" if default_no else "Answer [Y/n]:"]
    for y, line in enumerate(lines[:h-2]):
        stdscr.move(y, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(y, 0, line, w-1, curses.A_BOLD if y == 0 else 0)
    curses.echo(); curses.curs_set(1)
    prompt_y = min(len(lines), h-1)
    prompt = "Confirm: "
    stdscr.move(prompt_y, 0)
    stdscr.clrtoeol()
    stdscr.addnstr(prompt_y, 0, prompt, w-1)
    stdscr.refresh()
    ans = stdscr.getstr(prompt_y, len(prompt), 20).decode(errors="replace").strip().lower()
    curses.noecho(); curses.curs_set(0)
    if not ans:
        return not default_no
    return ans in ("y", "yes")


def confirm_action(stdscr, title, detail, yes_text="y"):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    detail_lines = str(detail).splitlines()
    lines = [title, ""] + detail_lines + ["", "Delete? [y/N]"]
    for y, line in enumerate(lines[:h-2]):
        stdscr.move(y, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(y, 0, line, w-1, curses.A_BOLD if y == 0 else 0)
    curses.echo(); curses.curs_set(1)
    prompt_y = min(len(lines), h-1)
    prompt = "Confirm: "
    stdscr.move(prompt_y, 0)
    stdscr.clrtoeol()
    stdscr.addnstr(prompt_y, 0, prompt, w-1)
    stdscr.refresh()
    ans = stdscr.getstr(prompt_y, len(prompt), 20).decode(errors="replace").strip().lower()
    curses.noecho(); curses.curs_set(0)
    return ans in ("y", "yes")


def prompt_resume_a64_inbox(stdscr, count):
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        lines = [
            "A64 inbox has cached downloads from the previous search.",
            "",
            f"Cached file count: {count}",
            "",
            "Y - resume cached A64 downloads",
            "N - delete cache and start a new A64 search",
            "C/Esc - cancel",
        ]
        for y, line in enumerate(lines[:h-1]):
            stdscr.addnstr(y, 0, line, w-1, curses.A_BOLD if y == 0 else 0)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('y'), ord('Y')):
            return "resume"
        if ch in (ord('n'), ord('N')):
            return "new"
        if ch in (ord('c'), ord('C'), 27, ord('q')):
            return "cancel"


def load_wishlist(path="wishlist.txt"):
    p = Path(path)
    if not p.exists():
        return []
    items = []
    for line in p.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        items.append(s)
    return items


def save_wishlist(items, path="wishlist.txt"):
    Path(path).write_text("\n".join(items) + ("\n" if items else ""))


def select_wishlist_item(stdscr, items):
    if not items:
        show_error_popup(stdscr, "Wishlist", "No wishlist entries found in wishlist.txt")
        return None
    pos = 0
    top = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 3)
        if pos < top:
            top = pos
        if pos >= top + body_h:
            top = pos - body_h + 1
        top = max(0, min(top, max(0, len(items) - body_h)))
        stdscr.addnstr(0, 0, "Wishlist -> A64 search", w-1, curses.A_BOLD)
        stdscr.addnstr(1, 0, "Enter opens A64 search with only Name populated; cache is cleared first.", w-1, curses.A_DIM)
        for y, idx in enumerate(range(top, min(len(items), top + body_h)), start=2):
            attr = curses.A_REVERSE if idx == pos else 0
            stdscr.addnstr(y, 0, items[idx], w-1, attr)
        footer = "↑/↓ move  Enter search  x remove  q/Esc cancel"
        if len(items) > body_h:
            footer += f"  {top+1}-{min(len(items), top+body_h)}/{len(items)}"
        stdscr.addnstr(h-1, 0, footer, w-1, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), 27):
            return None
        if ch in (10, 13):
            return items[pos]
        if ch == ord('x'):
            item = items[pos]
            if prompt_yes_no(stdscr, "Remove wishlist item", f"Remove from current wishlist?\n\n{item}\n\nThis does not change wishlist_all.txt.", default_no=True):
                del items[pos]
                save_wishlist(items)
                if not items:
                    return None
                pos = min(pos, len(items) - 1)
                top = min(top, max(0, len(items) - 1))
            continue
        if ch == curses.KEY_UP:
            pos = max(0, pos - 1)
        elif ch == curses.KEY_DOWN:
            pos = min(len(items) - 1, pos + 1)
        elif ch == curses.KEY_PPAGE:
            pos = max(0, pos - body_h)
        elif ch == curses.KEY_NPAGE:
            pos = min(len(items) - 1, pos + body_h)
        elif ch == curses.KEY_HOME:
            pos = 0
        elif ch == curses.KEY_END:
            pos = len(items) - 1


def load_a64_inbox_rows(state_path):
    reconcile_a64_inbox(state_path)
    with a64_db_connect(state_path) as conn:
        rows = conn.execute(
            f"""
            SELECT title, result_title, a64_name, local_path AS path, local_path AS payload, 'local' AS mode,
                   file_type AS type, file_type, original_filename, group_name, year,
                   entries_count, entries_checked_at,
                   a64_id, a64_category, entry_index, size_bytes, local_path,
                   downloaded_at, a64_status AS status, a64_status
            FROM a64_download_rows
            WHERE local_path != '' AND downloaded_at IS NOT NULL
              AND COALESCE(a64_status, '') NOT IN ('deleted', 'discarded', 'promoted')
            {A64_INBOX_ORDER_BY}
            """
        ).fetchall()
    return [dict(r) for r in rows]


def enter_a64_inbox(state_path):
    inbox_rows = load_a64_inbox_rows(state_path)
    return inbox_rows, "A64 inbox", None, None, 0, 0, f"A64 inbox: {len(inbox_rows)} candidate(s)"


def a64_search_flow(stdscr, state_path, initial_data=None):
    data = dict(initial_data or {})
    client = Assembly64Client()
    page_count = 50
    while True:
        data = a64_search_form(stdscr, data)
        if not data:
            return "A64 search cancelled"
        types = [t.strip() for t in data.get("types", "").split(",") if t.strip()]
        query = build_aql(name=data.get("name", ""), types=types, category=data.get("category", ""), subcat=data.get("subcat", ""), repo=data.get("repo", ""), group=data.get("group", ""), handle=data.get("handle", ""), sort=data.get("sort", ""), order=data.get("order", ""), latest=data.get("latest", ""))
        page_start = 0
        try:
            results = client.search_aql(query, page_start, page_count)
        except Exception as e:
            return f"A64 search failed: {e}"
        with a64_db_connect(state_path) as conn:
            for r in results:
                a64_upsert_result(conn, r)
        if results:
            break
        show_error_popup(stdscr, "A64 search", f"No results for:\n\n{query}\n\nRefine the criteria and search again.")

    selected = set()
    entry_cache = {}
    marker_cache = a64_result_marker_map(state_path, results)
    payload_cache = a64_result_payload_map(state_path, results)
    pos = 0
    top = 0
    msg = f"A64: {len(results)} result(s); Space marks, d downloads marked, a downloads all"
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 5)
        if pos < top:
            top = pos
        if pos >= top + body_h:
            top = pos - body_h + 1
        top = max(0, min(top, max(0, len(results) - body_h)))
        stdscr.addnstr(0, 0, f"A64 search: {query}", w-1, curses.A_BOLD)
        hint_attr = (curses.color_pair(5) | curses.A_BOLD) if curses.has_colors() else curses.A_BOLD
        stdscr.addnstr(1, 0, "Icons: 💾 promoted  👓 tested  ☎ cached  ↓ prior download/missing", w-1, hint_attr)
        stdscr.addnstr(2, 0, "S  St  #    ID/category          Year  # Files    Title", w-1, curses.A_DIM)
        for y, idx in enumerate(range(top, min(len(results), top + body_h)), start=3):
            r = results[idx]
            rid = str(r.get("id", "")); cat = int(r.get("category") or 0)
            key = (rid, cat)
            suffix = ""
            if key in entry_cache:
                entries = entry_cache[key]
                suffix = f" | {file_set_label(entries)} | type(s): {entry_type_summary(entries)}"
            mark = "*" if idx in selected else " "
            status_mark = marker_cache.get(key, " ")
            attr = curses.A_REVERSE if idx == pos else 0
            # Draw status in fixed columns so wide emoji do not shift titles unpredictably.
            stdscr.addnstr(y, 0, mark, 1, attr)
            stdscr.addnstr(y, 3, status_mark if status_mark.strip() else " ", 2, attr)
            stdscr.addnstr(y, 7, f"{idx+1:3}.", 4, attr)
            stdscr.addnstr(y, 13, f"{rid}/{cat}", 18, attr)
            stdscr.addnstr(y, 33, str(r.get('year',''))[:4], 4, attr)
            payload_info = payload_cache.get(key)
            files_attr = attr
            if payload_info is None:
                payload_note = "?"
                files_attr |= curses.A_DIM
            elif payload_info.get("http_status") and int(payload_info.get("http_status") or 0) >= 400:
                payload_note = str(payload_info.get("http_status"))
                if curses.has_colors():
                    files_attr |= curses.color_pair(2)
            else:
                payload_note = str(payload_info.get("count", 0))
                if curses.has_colors():
                    files_attr |= curses.color_pair(5)
            stdscr.addnstr(y, 39, f"{payload_note[:7]:<9}", 9, files_attr)
            title = str(r.get('name',''))
            group = str(r.get('group',''))
            if group:
                title += f" [{group}]"
            if suffix:
                title += suffix
            stdscr.addnstr(y, 50, title, max(1, w-51), attr)
        stdscr.addnstr(h-2, 0, msg, w-1)
        footer = "↑/↓ move  Space mark  e entries  d download  a all  n/p page  q/Esc cancel"
        stdscr.addnstr(h-1, 0, footer, w-1, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), 27):
            return "A64 search cancelled"
        if ch == curses.KEY_UP:
            pos = max(0, pos - 1)
        elif ch == curses.KEY_DOWN:
            pos = min(len(results) - 1, pos + 1)
        elif ch == curses.KEY_PPAGE:
            pos = max(0, pos - body_h)
        elif ch == curses.KEY_NPAGE:
            pos = min(len(results) - 1, pos + body_h)
        elif ch == curses.KEY_HOME:
            pos = 0
        elif ch == curses.KEY_END:
            pos = len(results) - 1
        elif ch == ord(' '):
            selected.symmetric_difference_update({pos})
        elif ch in (ord('n'), ord('p')):
            new_start = page_start + page_count if ch == ord('n') else max(0, page_start - page_count)
            if new_start == page_start:
                msg = "Already at first A64 page"
                continue
            try:
                new_results = client.search_aql(query, new_start, page_count)
                if not new_results:
                    msg = "No more A64 results"
                    continue
                results = new_results
                page_start = new_start
                selected.clear(); entry_cache.clear(); pos = 0; top = 0
                with a64_db_connect(state_path) as conn:
                    for r in results:
                        a64_upsert_result(conn, r)
                marker_cache = a64_result_marker_map(state_path, results)
                payload_cache = a64_result_payload_map(state_path, results)
                msg = f"A64 results {page_start+1}-{page_start+len(results)}"
            except Exception as e:
                msg = f"A64 page failed: {e}"
        elif ch == ord('e'):
            r = results[pos]; key = (str(r.get("id", "")), int(r.get("category") or 0))
            try:
                entry_cache[key] = client.entries(*key)
                with a64_db_connect(state_path) as conn:
                    result_pk = a64_upsert_result(conn, r)
                    a64_record_entries_check(conn, result_pk, 200, len(entry_cache[key]))
                payload_cache[key] = f"{len(entry_cache[key])} file{'s' if len(entry_cache[key]) != 1 else ''} in payload"
                msg = f"Fetched {len(entry_cache[key])} entr{'y' if len(entry_cache[key]) == 1 else 'ies'} for {r.get('name','')}"
            except Exception as e:
                msg = f"A64 entries failed: {e}"
        elif ch in (ord('a'), ord('d')):
            indexes = range(len(results)) if ch == ord('a') else sorted(selected)
            indexes = list(indexes)
            if not indexes:
                msg = "No A64 results marked"
                continue
            downloaded = 0
            failed = 0
            no_entries = 0
            total = len(indexes)
            for n, idx in enumerate(indexes, 1):
                r = results[idx]
                rid = str(r.get("id", "")); cat = int(r.get("category") or 0); key = (rid, cat)
                try:
                    entries = entry_cache.get(key) or client.entries(rid, cat)
                    entry_cache[key] = entries
                    with a64_db_connect(state_path) as conn:
                        result_pk = a64_upsert_result(conn, r)
                        a64_record_entries_check(conn, result_pk, 200, len(entries))
                        payload_cache[key] = f"{len(entries)} file{'s' if len(entries) != 1 else ''} in payload"
                        for e in entries:
                            a64_upsert_entry(conn, result_pk, e)
                    # Download the whole A64 payload, not just U2-launchable files.
                    # Non-launchable entries (txt/md/nfo/source/etc.) stay selectable and
                    # will be handled by the defensive local viewer rather than launcher.
                    grab = entries
                    if not grab:
                        no_entries += 1
                        continue
                    for e_i, e in enumerate(grab, 1):
                        out = DEFAULT_INBOX / safe_inbox_filename(rid, cat, e)
                        reference_only = e.suffix.lower().lstrip('.') in REFERENCE_ONLY_TYPES
                        # Inline progress view: dim the page, highlight the row being fetched.
                        stdscr.erase()
                        h, w = stdscr.getmaxyx()
                        action = "referencing" if reference_only else "downloading"
                        stdscr.addnstr(0, 0, f"A64 {action} {n}/{total}: {r.get('name','')} -> {Path(out).name}", w-1, curses.A_BOLD)
                        bar_w = max(10, min(w - 20, 50))
                        done = int(bar_w * ((n - 1) / max(1, total)))
                        bar = "[" + "#" * done + "." * (bar_w - done) + "]"
                        stdscr.addnstr(1, 0, f"{bar} result {n}/{total}, file {e_i}/{len(grab)}", w-1)
                        page_start = max(0, min(idx - 5, max(0, len(results) - max(1, h - 4))))
                        for y, j in enumerate(range(page_start, min(len(results), page_start + max(1, h - 4))), start=3):
                            rr = results[j]
                            line = f"  {j+1:3}. {rr.get('name','')} | {rr.get('group','')} | {rr.get('year','')} | id={rr.get('id','')} cat={rr.get('category','')}"
                            attr = curses.A_DIM
                            if j == idx:
                                attr = curses.A_BOLD | (curses.color_pair(1) if curses.has_colors() else 0)
                            stdscr.addnstr(y, 0, line, w-1, attr)
                        stdscr.refresh()
                        if reference_only:
                            # TAP/T64 are retained as A64 references only for now. They
                            # are not show-launchable and conversion is non-trivial, so
                            # keep a touched placeholder instead of spending time/cache
                            # downloading tape/container payloads.
                            Path(out).parent.mkdir(parents=True, exist_ok=True)
                            Path(out).touch()
                        else:
                            client.download_entry(rid, cat, e.entry_index, out)
                        with a64_db_connect(state_path) as conn:
                            result_pk = a64_upsert_result(conn, r)
                            a64_upsert_entry(conn, result_pk, e, str(out))
                        downloaded += 1
                except Exception:
                    failed += 1
            parts = [f"A64 downloaded {downloaded} file(s)"]
            if no_entries:
                parts.append(f"{no_entries} result(s) had no entries")
            if failed:
                parts.append(f"{failed} result(s) failed")
            return "; ".join(parts)


def safe_byte_view_lines(data: bytes, width: int):
    """Render arbitrary bytes defensively for curses; never emit raw controls."""
    width = max(1, width)
    lines = []
    cur = ""
    for b in data:
        if b in (10, 13):
            if b == 13:
                continue
            lines.append(cur)
            cur = ""
            continue
        if b == 9:
            ch = "    "
        elif 32 <= b <= 126:
            ch = chr(b)
        elif b >= 128:
            # Latin-1 keeps high-byte docs somewhat readable; nonprintables become dots.
            ch = bytes([b]).decode("latin-1", "replace")
            if not ch.isprintable():
                ch = SAFE_BYTE_DOT
        else:
            ch = SAFE_BYTE_DOT
        for c in ch:
            if len(cur) >= width:
                lines.append(cur)
                cur = ""
            cur += c if c.isprintable() else SAFE_BYTE_DOT
    lines.append(cur)
    return lines or [""]


def show_local_file_viewer(stdscr, path):
    p = Path(path)
    try:
        data = p.read_bytes()
    except Exception as e:
        show_error_popup(stdscr, "View file", f"Could not read {p}:\n\n{e}")
        return
    top = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        header = f"View: {p.name}  {len(data)} bytes"
        body_h = max(1, h - 3)
        lines = safe_byte_view_lines(data, max(1, w - 1))
        max_top = max(0, len(lines) - body_h)
        top = max(0, min(top, max_top))
        stdscr.addnstr(0, 0, header, w-1, curses.A_BOLD)
        stdscr.addnstr(1, 0, "Unsafe/control bytes shown as " + SAFE_BYTE_DOT, w-1, curses.A_DIM)
        for y, line in enumerate(lines[top:top + body_h], start=2):
            stdscr.addnstr(y, 0, line, w-1)
        footer = "↑/↓ scroll  PgUp/PgDn page  Home/End  q/Esc/Enter return"
        if len(lines) > body_h:
            footer += f"  {top+1}-{min(len(lines), top+body_h)}/{len(lines)}"
        stdscr.addnstr(h-1, 0, footer, w-1, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), 27, 10, 13):
            return
        if ch == curses.KEY_UP:
            top -= 1
        elif ch == curses.KEY_DOWN:
            top += 1
        elif ch == curses.KEY_PPAGE:
            top -= body_h
        elif ch == curses.KEY_NPAGE:
            top += body_h
        elif ch == curses.KEY_HOME:
            top = 0
        elif ch == curses.KEY_END:
            top = max_top


def show_help(stdscr):
    lines = [
        "Ultimate2+ NFC Curator Help",
        "",
        "Navigation:",
        "  Up/Down arrows  Move selection",
        "  PgUp/PgDn       Move one page",
        "  Home/End        Jump first/last item",
        "  /                Plain-text search/filter; blank clears",
        "  V                Choose SQL view/filter",
        "  H                Hunt/select row by tapping an NFC card",
        "  y                Search/download Assembly64 candidates",
        "  Shift-W          Wishlist item -> A64 search; clears local A64 cache",
        "  $                Show selected disk directory",
        "",
        "Assembly64:",
        "  Search form:     Up/Down move, Enter edit, Space cycles combo, c clears, s searches",
        "  Results:         Space marks, e fetches entries, d downloads marked, a downloads all visible",
        "                   n/p pages through additional A64 result pages",
        "  Inbox:           t/T test, f failed, x delete local candidate, u clear status",
        "                   v/Enter views local non-launchable/readme files defensively",
        "                   F10 converts local A64 PRG/disk candidates inside the cache",
        "                   p promotes to /A64, marks approved, then offers NFC write/verify",
        "  Result markers:  ☎ downloaded locally, 👓 tested/viewed, 💾 promoted to U2/permanent storage",
        "  Downloads:       Saved under local a64_inbox/ and recorded in SQLite",
        "",
        "Game status/actions:",
        "  Space or a       Approve/unapprove selected game",
        "  t                Test launch selected game",
        "  T                Test launch, bypass prehelp",
        "  f                Mark failed",
        "  x                Delete selected image/candidate after confirmation",
        "  u                Clear status/unmark",
        "  1  (digit one)   Toggle target machine mode: C64 <-> C128",
        "",
        "NFC:",
        "  w                Write selected game's NFC card",
        "  r                Read NFC tag only; do not launch",
        "  m                Start NFC monitor/launcher",
        "",
        "Editing/saving:",
        "  k                Edit post-launch key script for selected image",
        "  e                Edit soft title, launch mode/entry (* clears entry)",
        "  G  (capital G)   Send GO64, wait for prompt, then send Y",
        "  B  (capital B)   Reboot Ultimate/machine via API",
        "  C  (capital C)   Open simple keyboard command console",
        "  M  (capital M)   Mount selected image into drive A/8",
        "  D  (capital D)   Reserved for future disk swap workflow",
        "  q                Save and quit",
        "  ?                Show this help",
        "",
        "Note: '1' is the digit one, not lowercase L.",
        "Press any key to return.",
    ]
    top = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 1)
        max_top = max(0, len(lines) - body_h)
        top = max(0, min(top, max_top))
        for y, line in enumerate(lines[top:top + body_h]):
            attr = curses.A_BOLD if (top + y) == 0 else 0
            stdscr.addnstr(y, 0, line, w-1, attr)
        footer = "↑/↓ scroll  PgUp/PgDn page  Home/End  q/Esc/Enter return"
        if len(lines) > body_h:
            footer += f"  {top+1}-{min(len(lines), top+body_h)}/{len(lines)}"
        stdscr.addnstr(h-1, 0, footer, w-1, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), 27, 10, 13, ord(' ')):
            return
        if ch == curses.KEY_UP:
            top -= 1
        elif ch == curses.KEY_DOWN:
            top += 1
        elif ch == curses.KEY_PPAGE:
            top -= body_h
        elif ch == curses.KEY_NPAGE:
            top += body_h
        elif ch == curses.KEY_HOME:
            top = 0
        elif ch == curses.KEY_END:
            top = max_top


def refresh_status_rows(host):
    try:
        return drive_status_rows(host), None
    except Exception as e:
        return [], f"Drive status unavailable: {e}"


def draw_drive_status(stdscr, y, w, rows, err=None):
    if err:
        stdscr.addnstr(y, 0, err, w-1, curses.color_pair(3) if curses.has_colors() else 0)
        return 1
    for offset, (label, bus, enabled, dtype, mounted, last_error) in enumerate(rows[:3]):
        x = 0
        name = f"Drive {label}/{bus:>2}: "
        stdscr.addnstr(y + offset, x, name, max(0, w-1-x)); x += len(name)
        state_word = "Enabled" if enabled else "Disabled"
        attr = curses.color_pair(1) if enabled else curses.color_pair(2)
        if not curses.has_colors():
            attr = 0
        stdscr.addnstr(y + offset, x, state_word, max(0, w-1-x), attr); x += len(state_word)
        if enabled:
            rest = f" {dtype} mode"
            if mounted:
                rest += f" | Mounted: {mounted}"
            stdscr.addnstr(y + offset, x, rest, max(0, w-1-x)); x += len(rest)
        if last_error:
            err_text = f" | Error: {last_error}"
            attr = curses.color_pair(3) if curses.has_colors() else 0
            stdscr.addnstr(y + offset, x, err_text, max(0, w-1-x), attr)
    return min(len(rows), 3)


def draw_detail_panel(stdscr, y, w, row, highlight=None):
    target = "c128" if row.get("machine_mode", "c64") in ("c128", "128") else "c64"
    kind = (row.get("type") or row.get("file_type") or row.get("mode") or "").upper()
    fields = [
        ("target", "target", target),
        ("type", "type", kind),
        ("mode", "launch mode", row.get("mode", "")),
        ("entry", "entry", row.get("entry", "")),
        ("state", "state", row.get("status", "")),
    ]
    x = 0
    for key, name, value in fields:
        text = f" {name}: {value or '-'} "
        attr = curses.A_REVERSE if highlight == key else curses.A_NORMAL
        stdscr.addnstr(y, x, text, max(0, w-1-x), attr)
        x += len(text)
        if x >= w - 1:
            return
    path = f" path: {row.get('path', '')}"
    attr = curses.A_REVERSE if highlight == "path" else curses.A_NORMAL
    stdscr.addnstr(y + 1, 0, path, w-1, attr)


def selected_issue(host, row, cache):
    """Return human-readable issue for selected row, or empty string."""
    path = row.get("path", "")
    lower = path.lower()
    if not lower.endswith(".prg"):
        return ""
    if path and not path.startswith("/"):
        try:
            return "File problem: PRG is 0 bytes" if Path(path).stat().st_size == 0 else ""
        except Exception as e:
            return f"File problem: cannot stat local PRG ({e})"
    if path in cache:
        return cache[path]
    try:
        # ftp_size may emit expected Usb0/Usb1 fallback notes; suppress them
        # because this runs during screen redraw and would corrupt curses layout.
        with contextlib.redirect_stdout(io.StringIO()):
            size = ftp_size(host, path)
        cache[path] = "File problem: PRG is 0 bytes" if size == 0 else ""
    except Exception as e:
        cache[path] = f"File problem: cannot stat PRG ({e})"
    return cache[path]


def safe_addnstr(stdscr, y, x, text, n, attr=0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w or n <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, min(n, w - 1 - x), attr)
    except curses.error:
        pass


def command_groups(row=None, issue=""):
    path = (row or {}).get("path", "")
    is_disk = path.lower().endswith((".d64", ".d71", ".d81"))
    is_a64 = is_a64_row(row or {})
    can_test = not issue
    if is_a64:
        return [
            ("Nav", [("↑/↓ move", True), ("PgUp/PgDn", True), ("Home/End", True), ("/ search", True), ("V view", True), ("y A64", True), ("? help", True), ("q save+quit", True)]),
            ("A64", [("t test", can_test), ("T test no help", can_test), ("F10 actions", True), ("p promote", True), ("f failed", True), ("x delete", True), ("u unmark", True)]),
            ("U2", [("M mount image", is_disk), ("G GO64/Y", True), ("B reboot", True), ("C console", True)]),
            ("Edit", [("e title/entry", True)]),
        ]
    return [
        ("Nav", [("↑/↓ move", True), ("PgUp/PgDn", True), ("Home/End", True), ("/ search", True), ("V view", True), ("H hunt tag", True), ("y A64", True), ("$ dir", is_disk), ("? help", True), ("q save+quit", True)]),
        ("Curate", [("Space/a approve", True), ("t test", can_test), ("T test no help", can_test), ("1 64/128", True), ("f failed", True), ("x delete", True), ("u unmark", True)]),
        ("NFC", [("w write", True), ("r read", True), ("m monitor", True)]),
        ("U2", [("M mount image", is_disk), ("D disk swap", False), ("G GO64/Y", True), ("B reboot", True), ("C console", True)]),
        ("Edit", [("k script", True), ("e title/entry", True)]),
    ]


def command_panel_rows(h, w, row=None, issue=""):
    if w < 100:
        return 0
    groups = command_groups(row, issue)
    required_rows = 1 + sum(1 + len(items) for _, items in groups)
    return 2 if h < required_rows else required_rows


def draw_command_panel(stdscr, w, row=None, issue=""):
    h, _ = stdscr.getmaxyx()
    groups = command_groups(row, issue)
    # Put a compact command list on the right when there is room. Otherwise the
    # normal status/help area remains minimal and '?' has the full help.
    if w < 100:
        return 2
    x = max(45, w - 34)
    y = 0
    required_rows = 1 + sum(1 + len(items) for _, items in groups)
    safe_addnstr(stdscr, y, x, "Commands", max(0, w-1-x), curses.A_BOLD); y += 1
    if h < required_rows:
        safe_addnstr(stdscr, y, x + 2, "? help", max(0, w-1-(x+2)))
        return 4
    for title, items in groups:
        safe_addnstr(stdscr, y, x, f"{title}:", max(0, w-1-x), curses.A_BOLD); y += 1
        for item, enabled in items:
            attr = curses.A_NORMAL if enabled else (curses.A_DIM | (curses.color_pair(4) if curses.has_colors() else 0))
            safe_addnstr(stdscr, y, x + 2, item, max(0, w-1-(x+2)), attr); y += 1
    return 4


def natural_sort_key(text):
    parts = re.split(r"(\d+)", str(text).lower())
    return [int(p) if p.isdigit() else p for p in parts]


def row_sort_key(row):
    return (
        natural_sort_key(row.get("title", "")),
        natural_sort_key(row.get("path", "")),
    )


def row_matches_search(row, query):
    if not query:
        return True
    q = query.lower()
    hay = "\n".join(str(row.get(k, "")) for k in (
        "title", "path", "payload", "detail", "type", "file_type", "mode", "entry", "notes"
    )).lower()
    return q in hay


def row_identity(row):
    return row.get("path") or row.get("payload") or row.get("title", "")


DEFAULT_SQL_VIEWS = [
    ("Normal active rows", "SELECT * FROM image_rows WHERE COALESCE(storage_status, 'present') = 'present' AND COALESCE(quarantined, 0) = 0 AND COALESCE(file_type_enabled, 1) = 1 ORDER BY title, path"),
    ("Approved", "SELECT * FROM image_rows WHERE status = 'approved' ORDER BY title, path"),
    ("Failed", "SELECT * FROM image_rows WHERE status = 'failed' ORDER BY title, path"),
    ("C64", "SELECT * FROM image_rows WHERE machine_mode IN ('', 'c64', '64') ORDER BY title, path"),
    ("C128", "SELECT * FROM image_rows WHERE machine_mode IN ('c128', '128') ORDER BY title, path"),
    ("CRT", "SELECT * FROM image_rows WHERE file_type = 'crt' OR path LIKE '%.crt' ORDER BY title, path"),
]


def sql_title_from_file(path):
    first = Path(path).read_text(errors="replace").splitlines()[0:1]
    if first and first[0].lstrip().startswith("--"):
        title = first[0].lstrip()[2:].strip()
        if title:
            return title
    return Path(path).stem


def load_sql_view_choices():
    choices = [("All loaded rows", None, "Clear SQL view/filter")]
    choices.extend((name, sql, "Built-in") for name, sql in DEFAULT_SQL_VIEWS)
    sql_dir = Path("sql")
    if sql_dir.exists():
        for p in sorted(sql_dir.glob("*.sql")):
            try:
                choices.append((sql_title_from_file(p), p.read_text(errors="replace"), str(p)))
            except Exception:
                pass
    return choices


def show_error_popup(stdscr, title, message):
    lines = [title, ""] + str(message).splitlines() + ["", "Press any key to continue."]
    top = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 1)
        max_top = max(0, len(lines) - body_h)
        top = max(0, min(top, max_top))
        for y, line in enumerate(lines[top:top + body_h]):
            abs_i = top + y
            attr = curses.A_BOLD if abs_i == 0 else 0
            if abs_i == 0 and curses.has_colors():
                attr |= curses.color_pair(2)
            stdscr.addnstr(y, 0, line, w-1, attr)
        footer = "↑/↓ scroll  PgUp/PgDn page  Home/End  any key return"
        if len(lines) > body_h:
            footer += f"  {top+1}-{min(len(lines), top+body_h)}/{len(lines)}"
        stdscr.addnstr(h-1, 0, footer, w-1, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == curses.KEY_UP:
            top -= 1
        elif ch == curses.KEY_DOWN:
            top += 1
        elif ch == curses.KEY_PPAGE:
            top -= body_h
        elif ch == curses.KEY_NPAGE:
            top += body_h
        elif ch == curses.KEY_HOME:
            top = 0
        elif ch == curses.KEY_END:
            top = max_top
        else:
            return


def select_sql_view(stdscr, current_name="All loaded rows"):
    choices = load_sql_view_choices()
    pos = next((i for i, c in enumerate(choices) if c[0] == current_name), 0)
    top = max(0, pos - 3)
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 3)
        if pos < top:
            top = pos
        if pos >= top + body_h:
            top = pos - body_h + 1
        stdscr.addnstr(0, 0, "Select SQL view", w-1, curses.A_BOLD)
        stdscr.addnstr(1, 0, "Built-ins plus sql/*.sql. First '-- comment' line becomes the title.", w-1, curses.A_DIM)
        for y, idx in enumerate(range(top, min(len(choices), top + body_h)), start=2):
            name, _sql, source = choices[idx]
            line = f"{name}  [{source}]"
            attr = curses.A_REVERSE if idx == pos else 0
            stdscr.addnstr(y, 0, line, w-1, attr)
        footer = "↑/↓ move  Enter apply  q/Esc cancel"
        stdscr.addnstr(h-1, 0, footer, w-1, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), 27):
            return None
        if ch in (10, 13):
            return choices[pos]
        if ch == curses.KEY_UP:
            pos = max(0, pos - 1)
        elif ch == curses.KEY_DOWN:
            pos = min(len(choices) - 1, pos + 1)
        elif ch == curses.KEY_PPAGE:
            pos = max(0, pos - body_h)
        elif ch == curses.KEY_NPAGE:
            pos = min(len(choices) - 1, pos + body_h)
        elif ch == curses.KEY_HOME:
            pos = 0
        elif ch == curses.KEY_END:
            pos = len(choices) - 1


def card_payload_path(text):
    if not text or not text.startswith("U2+:"):
        return ""
    body = text[4:]
    if ":" in body:
        mode, rest = body.split(":", 1)
        if mode in ("prg", "crt", "disk", "d64", "tap"):
            body = rest
    path = body.partition("#")[0]
    return strip_usb_prefix(path)


def read_one_nfc_payload(device="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0", baud=115200, timeout=20):
    if not open_serial:
        raise RuntimeError("NFC reader helpers unavailable")
    if not Path(device).exists() and Path("/dev/ttyUSB0").exists():
        device = "/dev/ttyUSB0"
    baud_const = getattr(termios, f"B{baud}")
    fd = open_serial(device, baud_const)
    try:
        require_response(fd, [0x14, 0x01], 0x15, timeout=1.0)
        deadline = time.time() + timeout
        last_uid = None
        while time.time() < deadline:
            uid = find_tag(fd)
            if uid and uid != last_uid:
                memory = read_ntag_memory(fd)
                text = decode_first_ndef_text_or_uri(parse_ndef_tlv(memory))
                if text:
                    return text
                last_uid = uid
            time.sleep(0.2)
    finally:
        os.close(fd)
    raise TimeoutError("Timed out waiting for NFC card")


def apply_sql_view_choice(state_path, base_rows, choice):
    name, sql, _source = choice
    if sql is None:
        return base_rows, None, None, name
    sql_filter_keys, sql_filter_rank, sql_rows = sql_view_result(state_path, sql)
    base_keys = {row_identity(r) for r in base_rows}
    if sql_filter_keys and sql_filter_keys.issubset(base_keys):
        return base_rows, sql_filter_keys, sql_filter_rank, name
    return sql_rows, None, None, name


def sql_view_result(db_path, sql):
    if sql is None:
        return None, None, None
    if not is_sqlite_path(db_path) or not Path(db_path).exists():
        raise RuntimeError(f"SQL views require an existing SQLite DB: {db_path}")
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        ensure_a64_tables(conn)
        result = conn.execute(sql).fetchall()
    ordered = []
    seen = set()
    sql_rows = []
    for row in result:
        d = dict(row)
        if not d.get("file_type") and d.get("type"):
            d["file_type"] = d.get("type", "")
        d.setdefault("status", d.get("a64_status", ""))
        d.setdefault("machine_mode", "c64")
        d.setdefault("notes", "")
        key = row_identity(d)
        sql_rows.append(d)
        if key and key not in seen:
            ordered.append(key)
            seen.add(key)
    return seen, {key: i for i, key in enumerate(ordered)}, sql_rows


def run(stdscr, rows, host, db_path, log_path, startup_msg=""):
    state_path = db_path  # A64 tables and curator rows live in the same SQLite DB.
    base_rows = rows
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_WHITE, -1)
        curses.init_pair(5, curses.COLOR_CYAN, -1)
    status_rows, status_err = refresh_status_rows(host)
    issue_cache = {}
    pos = 0
    top = 0
    msg = startup_msg or ""
    search_query = ""
    sql_view_name = "All loaded rows"
    sql_filter_keys = None
    sql_filter_rank = None
    saved_sql_view_name = load_tui_state().get("sql_view_name", "All loaded rows")
    for r in rows:
        if not r.get("file_type") and r.get("type"):
            r["file_type"] = r.get("type", "")
        if r.get("mode") == "d64":
            r["mode"] = "disk"
            r["payload"] = payload_for(r["mode"], r.get("path", ""), r.get("entry", ""))
        if r.get("mode") not in ("prg", "crt", "disk"):
            r["mode"] = "disk" if str(r.get("path", "")).lower().endswith((".d64", ".d71", ".d81")) else "prg"
            r["payload"] = payload_for(r["mode"], r.get("path", ""), r.get("entry", ""))
        r.setdefault("status", "")
        r.setdefault("notes", "")
        r.setdefault("machine_mode", "c64")
    if saved_sql_view_name != "All loaded rows":
        choice = next((c for c in load_sql_view_choices() if c[0] == saved_sql_view_name), None)
        if choice:
            try:
                rows, sql_filter_keys, sql_filter_rank, sql_view_name = apply_sql_view_choice(state_path, base_rows, choice)
                msg = f"SQL view restored: {sql_view_name}"
            except Exception as e:
                msg = f"Could not restore SQL view {saved_sql_view_name}: {e}"
    while True:
        visible = [
            i for i, r in enumerate(rows)
            if (sql_filter_keys is None or row_identity(r) in sql_filter_keys)
            and row_matches_search(r, search_query)
        ]
        if sql_filter_rank is not None:
            visible.sort(key=lambda i: sql_filter_rank.get(row_identity(rows[i]), 10**9))
        if visible and pos not in visible:
            pos = visible[0]
            top = 0
        current_row = rows[pos] if visible else {}
        current_issue = selected_issue(host, current_row, issue_cache) if visible else ""

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        title = "Ultimate2+ C64 curator"
        if sql_view_name != "All loaded rows":
            title += f"  view:{sql_view_name}"
        if search_query:
            title += f"  /{search_query}"
        stdscr.addnstr(0, 0, title, w-1, curses.A_BOLD)
        list_start = draw_command_panel(stdscr, w, current_row, current_issue)
        panel_x = max(45, w - 34) if w >= 100 else w
        left_w = max(1, panel_x - 1) if w >= 100 else w
        cmd_rows = command_panel_rows(h, w, current_row, current_issue)
        sep_width = max(1, panel_x - 1)
        if w >= 100:
            stdscr.addnstr(1, 0, "-" * sep_width, sep_width)
            list_start = 2
        else:
            stdscr.addnstr(2, 0, "-" * sep_width, sep_width)
            list_start = 3
        status_y = max(list_start + 1, h - 7)
        detail_y = max(list_start + 1, h - 3)
        list_h = max(1, status_y - list_start)
        if visible:
            vi = visible.index(pos)
            if vi < top:
                top = vi
            if vi >= top + list_h:
                top = vi - list_h + 1
        else:
            top = 0
        if not visible:
            text = "<No files found for search>" if search_query else "<No rows>"
            attr = curses.A_DIM | (curses.color_pair(4) if curses.has_colors() else 0)
            stdscr.addnstr(list_start, 0, text, w-1, attr)
        if sql_view_name == "A64 inbox" and visible:
            y = list_start
            last_section = None
            last_pkg = None
            for idx in visible[top:]:
                if y >= list_start + list_h:
                    break
                r = rows[idx]
                cnt = int(r.get("entries_count") or 1)
                section = "Packages" if cnt >= 2 else "Single File Images"
                pkg = (r.get("a64_id"), r.get("a64_category")) if cnt >= 2 else None
                if section != last_section:
                    if y >= list_start + list_h:
                        break
                    if section == "Packages" and y > list_start:
                        y += 1
                        if y >= list_start + list_h:
                            break
                    stdscr.addnstr(y, 0, section, left_w-1, curses.A_BOLD | curses.A_DIM)
                    y += 1
                    last_section = section
                    last_pkg = None
                if cnt >= 2 and pkg != last_pkg:
                    remaining_pkg_rows = sum(1 for j in visible if j >= idx and (rows[j].get("a64_id"), rows[j].get("a64_category")) == pkg)
                    blank_before = 1 if last_pkg is not None else 0
                    needed = blank_before + 1 + remaining_pkg_rows
                    available = (list_start + list_h) - y
                    # Package rendering is intentionally all-or-stop for packages
                    # after the first package in the visible window: don't hide a
                    # large package and then show smaller later packages out of
                    # context/order. If the first package itself is too large,
                    # show its header and as many entries as fit.
                    if last_pkg is not None and needed > available:
                        break
                    if blank_before:
                        y += 1
                    if y >= list_start + list_h:
                        break
                    partial = remaining_pkg_rows < cnt or needed > available
                    count_note = f"{cnt} files" if not partial else f"{cnt} files, partial view"
                    package_title = r.get('result_title') or r.get('a64_name') or r.get('title', '')
                    stdscr.addnstr(y, 0, f"  {package_title}  [package: {count_note}]", left_w-1, curses.A_BOLD)
                    y += 1
                    last_pkg = pkg
                if y >= list_start + list_h:
                    break
                mark = "✓" if r.get("status") == "approved" else ("✗" if r.get("status") == "failed" else " ")
                target = "128" if r.get("machine_mode", "c64") in ("c128", "128") else "64"
                kind = (r.get("type") or r.get("file_type") or r.get("mode") or "").upper()
                prefix = "    " if cnt >= 2 else ""
                line = f"{prefix}{idx+1:3d} [{mark}] [{target}] {r.get('title','')}  ({kind})"
                attr = curses.A_REVERSE if idx == pos else 0
                if kind.lower() not in LAUNCHABLE_TYPES:
                    attr |= curses.A_DIM
                stdscr.addnstr(y, 0, line, left_w-1, attr)
                y += 1
        else:
            for y, idx in enumerate(visible[top:top + list_h], start=list_start):
                r = rows[idx]
                mark = "✓" if r.get("status") == "approved" else ("✗" if r.get("status") == "failed" else " ")
                target = "128" if r.get("machine_mode", "c64") in ("c128", "128") else "64"
                kind = (r.get("type") or r.get("file_type") or r.get("mode") or "").upper()
                line = f"{idx+1:3d} [{mark}] [{target}] {r.get('title','')}  ({kind})"
                attr = curses.A_REVERSE if idx == pos else 0
                stdscr.addnstr(y, 0, line, left_w-1, attr)
        status_w = w if (w < 100 or status_y >= cmd_rows) else left_w
        detail_w = w if (w < 100 or detail_y >= cmd_rows) else left_w
        footer_w = w if (w < 100 or h - 1 >= cmd_rows) else left_w
        draw_drive_status(stdscr, status_y, status_w, status_rows, status_err)
        if visible:
            r = rows[pos]
            draw_detail_panel(stdscr, detail_y, detail_w, r)
        else:
            r = {}
        if visible:
            vi = visible.index(pos)
            count_text = f"{vi+1}/{len(visible)}"
            if search_query:
                count_text += f" filtered ({len(rows)} total)"
        else:
            count_text = f"0/{len(visible)}"
            if search_query:
                count_text += f" filtered ({len(rows)} total)"
        footer = f"{count_text}  DB: {db_path}"
        stdscr.addnstr(h-1, 0, footer, footer_w-1)
        x = len(footer)
        if current_issue and x < footer_w - 1:
            issue_text = f" | {current_issue}"
            attr = curses.color_pair(2) if curses.has_colors() else curses.A_BOLD
            stdscr.addnstr(h-1, x, issue_text, max(0, footer_w-1-x), attr)
            x += len(issue_text)
        if msg and x < footer_w - 1:
            msg_text = f" | {clean_msg(msg)}"
            stdscr.addnstr(h-1, x, msg_text, max(0, footer_w-1-x))
        stdscr.refresh()

        ch = stdscr.getch()
        if not visible and ch not in (ord('?'), ord('/'), ord('V'), ord('H'), ord('h'), ord('q'), ord('s'), ord('y'), ord('W'), curses.KEY_UP, curses.KEY_DOWN):
            msg = "No selected row; clear or change search"
            continue
        if ch == ord('?'):
            show_help(stdscr)
        elif ch in (ord('v'), 10, 13) and sql_view_name == "A64 inbox" and is_local_a64_row(rows[pos]):
            ft = (rows[pos].get("file_type") or rows[pos].get("type") or Path(rows[pos].get("path", "")).suffix.lstrip(".")).lower()
            if ft in LAUNCHABLE_TYPES and ch in (10, 13):
                msg = "Enter views non-launchable local files; use t to test launch this candidate"
            else:
                show_local_file_viewer(stdscr, rows[pos].get("path", ""))
                msg = "Returned from local file viewer"
        elif ch == curses.KEY_F0 + 10:
            if is_a64_row(rows[pos]):
                msg = a64_actions_menu(stdscr, state_path, rows[pos])
                if sql_view_name == "A64 inbox":
                    old_pos = pos
                    rows = load_a64_inbox_rows(state_path)
                    pos = min(old_pos, max(0, len(rows) - 1))
                    top = min(top, max(0, len(rows) - 1))
            else:
                old_pos = pos
                msg = normal_image_actions_menu(stdscr, host, state_path, rows[pos], base_rows)
                rows = base_rows
                if rows:
                    pos = min(old_pos, len(rows) - 1)
                    top = min(top, max(0, len(rows) - 1))
        elif ch == ord('$'):
            show_disk_directory(stdscr, host, rows[pos], allow_select=False)
            msg = "Returned from disk directory"
        elif ch == ord('/'):
            curses.echo(); curses.curs_set(1)
            prompt = "Search (blank clears): "
            stdscr.move(h-1, 0); stdscr.clrtoeol()
            stdscr.addnstr(h-1, 0, prompt, w-1)
            stdscr.refresh()
            search_query = stdscr.getstr(h-1, len(prompt), 80).decode(errors="replace").strip()
            curses.noecho(); curses.curs_set(0)
            top = 0
            msg = "Search cleared" if not search_query else f"Search: {search_query}"
        elif ch == ord('V'):
            choice = select_sql_view(stdscr, sql_view_name)
            if choice:
                name, sql, _source = choice
                try:
                    rows, sql_filter_keys, sql_filter_rank, sql_view_name = apply_sql_view_choice(state_path, base_rows, choice)
                    save_tui_state(sql_view_name=sql_view_name)
                    top = 0
                    pos = 0
                    msg = f"SQL view: {name}"
                except Exception as e:
                    msg = f"SQL view failed: {e}"
                    show_error_popup(stdscr, "SQL view failed", f"View: {name}\n\n{e}\n\nPrevious view remains active: {sql_view_name}")
        elif ch == ord('W'):
            if sql_view_name == "A64 inbox":
                msg = "Wishlist is not available inside A64 inbox; q returns to curator"
                continue
            wish = select_wishlist_item(stdscr, load_wishlist())
            if not wish:
                msg = "Wishlist cancelled"
                continue
            cleared = clear_a64_inbox(state_path)
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    remote_cleared = clean_a64_test_cache(host)
            except Exception as e:
                remote_cleared = 0
                msg = f"A64 scratch cleanup warning: {e}"
            search_msg = a64_search_flow(stdscr, state_path, initial_data={"name": wish})
            inbox = load_a64_inbox_rows(state_path)
            if inbox:
                rows, sql_view_name, sql_filter_keys, sql_filter_rank, top, pos, inbox_msg = enter_a64_inbox(state_path)
                msg = f"Wishlist {wish!r}; cleared {cleared} local/{remote_cleared} U2 scratch; {search_msg}; {inbox_msg}"
            else:
                msg = f"Wishlist {wish!r}; cleared {cleared} local/{remote_cleared} U2 scratch; {search_msg}"
        elif ch == ord('y'):
            if sql_view_name == "A64 inbox":
                msg = "Already in A64 inbox; q returns to curator"
                continue
            cached = reconcile_a64_inbox(state_path)
            if cached:
                action = prompt_resume_a64_inbox(stdscr, len(cached))
                if action == "resume":
                    rows, sql_view_name, sql_filter_keys, sql_filter_rank, top, pos, msg = enter_a64_inbox(state_path)
                elif action == "new":
                    n = clear_a64_inbox(state_path)
                    try:
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            remote_n = clean_a64_test_cache(host)
                    except Exception as e:
                        remote_n = 0
                        msg = f"A64 scratch cleanup warning: {e}"
                    search_msg = a64_search_flow(stdscr, state_path)
                    inbox = load_a64_inbox_rows(state_path)
                    if inbox:
                        rows, sql_view_name, sql_filter_keys, sql_filter_rank, top, pos, inbox_msg = enter_a64_inbox(state_path)
                        msg = f"Cleared {n} local/{remote_n} U2 scratch A64 file(s); {search_msg}; {inbox_msg}"
                    else:
                        msg = f"Cleared {n} local/{remote_n} U2 scratch A64 file(s); {search_msg}"
                else:
                    msg = "A64 cancelled"
            else:
                search_msg = a64_search_flow(stdscr, state_path)
                inbox = load_a64_inbox_rows(state_path)
                if inbox:
                    rows, sql_view_name, sql_filter_keys, sql_filter_rank, top, pos, inbox_msg = enter_a64_inbox(state_path)
                    msg = f"{search_msg}; {inbox_msg}"
                else:
                    msg = search_msg
        elif ch in (ord('H'), ord('h')):
            curses.endwin()
            try:
                print("Tap NFC card to hunt/select matching row...", flush=True)
                text = read_one_nfc_payload()
                path = card_payload_path(text)
                match = next((i for i, row in enumerate(rows) if strip_usb_prefix(row.get("path", "")) == path), None)
                if match is None:
                    msg = f"No row matched card payload: {text}"
                else:
                    pos = match
                    search_query = ""
                    sql_filter_keys = None
                    sql_filter_rank = None
                    sql_view_name = "All loaded rows"
                    top = 0
                    msg = f"Selected from card: {rows[pos].get('title','')}"
            except Exception as e:
                msg = f"NFC hunt failed: {e}"
        elif ch == curses.KEY_UP:
            if visible:
                vi = visible.index(pos)
                pos = visible[max(0, vi-1)]
            status_rows, status_err = refresh_status_rows(host)
        elif ch == curses.KEY_DOWN:
            if visible:
                vi = visible.index(pos)
                pos = visible[min(len(visible)-1, vi+1)]
            status_rows, status_err = refresh_status_rows(host)
        elif ch == curses.KEY_PPAGE:
            if visible:
                vi = visible.index(pos)
                pos = visible[max(0, vi - max(1, list_h - 1))]
            status_rows, status_err = refresh_status_rows(host)
        elif ch == curses.KEY_NPAGE:
            if visible:
                vi = visible.index(pos)
                pos = visible[min(len(visible)-1, vi + max(1, list_h - 1))]
            status_rows, status_err = refresh_status_rows(host)
        elif ch == curses.KEY_HOME:
            if visible:
                pos = visible[0]
            status_rows, status_err = refresh_status_rows(host)
        elif ch == curses.KEY_END:
            if visible:
                pos = visible[-1]
            status_rows, status_err = refresh_status_rows(host)
        elif ch in (ord(' '), ord('a')):
            rows[pos]["status"] = "" if rows[pos].get("status") == "approved" else "approved"
            set_image_status(db_path, rows[pos], rows[pos]["status"])
            msg = f"Status updated: {rows[pos].get('status') or 'unmarked'}"
        elif ch == ord('1'):
            cur = "c128" if rows[pos].get("machine_mode", "c64") in ("c128", "128") else "c64"
            rows[pos]["machine_mode"] = "c64" if cur == "c128" else "c128"
            set_image_machine_mode(db_path, rows[pos], rows[pos]["machine_mode"])
            msg = f"Target mode for {rows[pos].get('title','')} set to {rows[pos]['machine_mode']}"
        elif ch == ord('k'):
            script_path = ensure_script_section(rows[pos].get("path", ""))
            curses.endwin()
            ok, lint_msg = edit_script_with_lint(script_path, rows[pos].get("path", ""), rows[pos].get("machine_mode", "c64"))
            msg = f"Edited key script {script_path}" if ok else lint_msg
        elif ch == ord('w'):
            curses.endwin()
            ok = write_nfc_for_row(rows[pos])
            if ok:
                rows[pos]["status"] = "approved"
                set_image_status(db_path, rows[pos], "approved")
                msg = f"Wrote NFC for {rows[pos].get('title','')} and updated DB"
            else:
                msg = "NFC write cancelled"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('m'):
            curses.endwin()
            ok, info = ensure_nfc_reader_connected()
            if not ok:
                msg = f"NFC reader unavailable: {info}"
                status_rows, status_err = refresh_status_rows(host)
                continue
            monitor = Path(__file__).with_name("u2_tag_monitor.py")
            cmd = [sys.executable, str(monitor), "--ultimate", "auto", "--state", db_path]
            subprocess.call(cmd)
            msg = "Returned from NFC monitor"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('r'):
            curses.endwin()
            ok, info = ensure_nfc_reader_connected()
            if not ok:
                msg = f"NFC reader unavailable: {info}"
                status_rows, status_err = refresh_status_rows(host)
                continue
            monitor = Path(__file__).with_name("u2_tag_monitor.py")
            cmd = [sys.executable, str(monitor), "--ultimate", "auto", "--state", db_path, "--no-launch"]
            subprocess.call(cmd)
            msg = "Returned from NFC read-only monitor"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('f'):
            if update_a64_entry_state(state_path, rows[pos], "failed"):
                msg = f"Marked A64 candidate failed: {rows[pos].get('title','')}"
            else:
                rows[pos]["status"] = "failed"
                set_image_status(db_path, rows[pos], "failed")
                msg = f"Marked failed: {rows[pos].get('title','')}"
        elif ch == ord('p'):
            if is_a64_row(rows[pos]):
                try:
                    promoted_title = rows[pos].get('title','')
                    dest, promoted_row = promote_a64_candidate(stdscr, host, state_path, rows[pos], base_rows)
                    wrote_card = False
                    if prompt_yes_no(stdscr, "A64 promoted", f"Promoted to:\n{dest}\n\nWrite NFC card now?", default_no=True):
                        curses.endwin()
                        wrote_card = write_nfc_for_row(promoted_row)
                    if sql_view_name == "A64 inbox":
                        del rows[pos]
                        if rows:
                            pos = min(pos, len(rows) - 1)
                        else:
                            rows = base_rows
                            sql_view_name = "All loaded rows"
                            sql_filter_keys = None
                            sql_filter_rank = None
                            pos = 0
                    msg = f"Promoted A64 candidate {promoted_title} to {dest}" + ("; NFC written" if wrote_card else "")
                except Exception as e:
                    msg = f"A64 promote failed: {clean_msg(e)}"
            else:
                msg = "Promote only applies to A64 candidates"
        elif ch == ord('x'):
            if is_a64_row(rows[pos]):
                try:
                    deleted_title = rows[pos].get('title','')
                    local_path = rows[pos].get("local_path") or rows[pos].get("path", "")
                    promoted_path = rows[pos].get("promoted_path", "")
                    detail = f"Delete A64 candidate: {deleted_title}\n\nLocal: {local_path or '-'}\nU2: {promoted_path or '-'}"
                    if not confirm_action(stdscr, "Delete A64 candidate", detail):
                        msg = "A64 delete cancelled"
                        continue
                    if local_path and not str(local_path).startswith("/"):
                        Path(local_path).unlink(missing_ok=True)
                    if promoted_path:
                        try:
                            ftp_delete(host, promoted_path)
                        except Exception:
                            pass
                    update_a64_entry_state(state_path, rows[pos], "deleted")
                    old_pos = pos
                    if sql_view_name == "A64 inbox" or is_a64_row(rows[pos]):
                        del rows[pos]
                        if rows:
                            pos = min(old_pos, len(rows) - 1)
                        else:
                            rows = base_rows
                            sql_view_name = "All loaded rows"
                            sql_filter_keys = None
                            sql_filter_rank = None
                            pos = 0
                    msg = f"Deleted A64 candidate: {deleted_title}"
                except Exception as e:
                    msg = f"A64 delete failed: {e}"
            else:
                try:
                    old_pos = pos
                    ok, msg = delete_curator_image(stdscr, host, state_path, rows[pos], base_rows)
                    if ok:
                        rows = base_rows
                        if rows:
                            pos = min(old_pos, len(rows) - 1)
                        else:
                            pos = 0
                        top = min(top, max(0, len(rows) - 1))
                except Exception as e:
                    msg = f"Delete failed: {clean_msg(e)}"
        elif ch == ord('u'):
            if clear_a64_entry_state(state_path, rows[pos]):
                msg = f"Cleared A64 candidate status: {rows[pos].get('title','')}"
            else:
                rows[pos]["status"] = ""
        elif ch == ord('G'):
            captured_out = io.StringIO()
            captured_err = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
                    print("STEP manual GO64 request")
                    inject_keys(host, "go64\r", machine_mode="c128")
                    print("STEP wait for GO64 confirmation prompt")
                    if not wait_for_screen_text(host, "ARE YOU SURE", timeout=8.0):
                        print("STEP GO64 prompt not detected; sending Y anyway")
                    inject_keys(host, "y\r", machine_mode="c128")
                    print("STEP wait for C64 BASIC banner")
                    wait_for_screen_text(host, "COMMODORE 64 BASIC", timeout=8.0)
                msg = "Sent GO64/Y"
            except Exception as e:
                msg = f"GO64/Y failed: {e}"
                captured_err.write(str(e) + "\n")
            with open(log_path, "a") as log:
                log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} manual GO64/Y ---\n")
                if captured_out.getvalue():
                    log.write("[stdout]\n" + captured_out.getvalue())
                if captured_err.getvalue():
                    log.write("[stderr]\n" + captured_err.getvalue())
                log.write(f"[tui] {msg}\n")
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('B'):
            try:
                status, body = reboot_machine(host)
                msg = f"Cleared cartridge; reboot requested HTTP {status}: {body.strip()}"
            except Exception as e:
                msg = f"Reboot failed: {e}"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('M'):
            try:
                path = rows[pos].get("path", "")
                if not path.lower().endswith((".d64", ".d71", ".d81")):
                    msg = f"Mount skipped: not a supported disk image"
                else:
                    mount_path = path
                    if is_local_a64_row(rows[pos]):
                        local = Path(path)
                        mount_path = "/Usb0/_A64_Test/" + local.name
                        h, w = stdscr.getmaxyx()
                        stdscr.move(h-1, 0); stdscr.clrtoeol()
                        stdscr.addnstr(h-1, 0, f"Uploading A64 disk for mount: {mount_path}", w-1)
                        stdscr.refresh()
                        mount_path = ftp_upload(host, mount_path, local.read_bytes())
                    # mount_image may print USB0/USB1 fallback notes; capture them so
                    # they do not corrupt the curses screen.
                    captured = io.StringIO()
                    with contextlib.redirect_stdout(captured):
                        status, body = mount_image(host, mount_path, "a")
                    msg = f"Mounted selected image to A/8: {mount_path}"
            except Exception as e:
                msg = f"Mount failed: {clean_msg(e)}"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('D'):
            msg = "Disk swap workflow is reserved/not implemented yet"
        elif ch == ord('C'):
            curses.endwin()
            command_console(host, rows[pos])
            msg = "Returned from keyboard command console"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('s'):
            msg = "Save is automatic/on exit; explicit Save is retired"
        elif ch in (ord('t'), ord('T')):
            issue = selected_issue(host, rows[pos], issue_cache)
            if issue:
                msg = issue
                continue
            def launch_status(text, kind="normal"):
                try:
                    hh, ww = stdscr.getmaxyx()
                    stdscr.move(hh - 1, 0)
                    stdscr.clrtoeol()
                    attr = curses.A_BOLD
                    if curses.has_colors():
                        attr |= curses.color_pair(5 if kind == "work" else 3)
                    stdscr.addnstr(hh - 1, 0, f"Launching: {clean_msg(text)}", ww - 1, attr)
                    stdscr.refresh()
                except Exception:
                    pass

            # Keep curses clean: capture noisy helper output and append it to a log.
            captured_out = io.StringIO()
            captured_err = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
                    target_mode = rows[pos].get("machine_mode", "c64") or "c64"
                    skip_prehelp = ch == ord('T')
                    if is_local_a64_row(rows[pos]):
                        status, body = launch_local_a64_candidate(stdscr, host, rows[pos], target_mode=target_mode, skip_prehelp=skip_prehelp, status_callback=launch_status)
                        update_a64_entry_state(state_path, rows[pos], "tested")
                    else:
                        status, body = launch_payload(host, rows[pos]["payload"], d64_as_prg_loader=True, target_mode=target_mode, skip_prehelp=skip_prehelp, status_callback=launch_status)
                # Do NOT mark approved/selected here. User must press Space/a after visual confirmation.
                msg = f"Test launched HTTP {status}: {body.strip()} -- if good, press Space/a to approve"
                if ch == ord('T'):
                    msg = "No-help " + msg
            except KeyboardInterrupt:
                msg = "Launch interrupted"
                captured_err.write("KeyboardInterrupt\n")
            except Exception as e:
                # Do NOT mark failed automatically; user can press f or just move on.
                msg = f"Launch failed: {e}"
                captured_err.write(str(e) + "\n")
            with open(log_path, "a") as log:
                log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} test launch: {rows[pos].get('title','')} ---\n")
                log.write(f"payload: {rows[pos].get('payload','')}\n")
                if captured_out.getvalue():
                    log.write("[stdout]\n" + captured_out.getvalue())
                if captured_err.getvalue():
                    log.write("[stderr]\n" + captured_err.getvalue())
                log.write(f"[tui] {msg}\n")
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('e'):
            curses.echo(); curses.curs_set(1)
            prompt = f"New soft title (blank keeps {rows[pos].get('title','')!r}): "
            stdscr.move(h-1, 0); stdscr.clrtoeol()
            stdscr.addnstr(h-1, 0, prompt, w-1)
            stdscr.refresh()
            title = stdscr.getstr(h-1, min(len(prompt), w-2), 120).decode(errors="replace").strip()

            # Highlight the exact field being edited in the detail panel.
            draw_detail_panel(stdscr, detail_y, w, rows[pos], highlight="mode")
            prompt = "New launch mode (prg/crt/disk; blank keeps current): "
            stdscr.move(h-1, 0); stdscr.clrtoeol()
            stdscr.addnstr(h-1, 0, prompt, w-1)
            stdscr.refresh()
            mode = stdscr.getstr(h-1, len(prompt), 20).decode(errors="replace").strip()

            stdscr.move(h-1, 0); stdscr.clrtoeol()
            draw_detail_panel(stdscr, detail_y, w, rows[pos], highlight="entry")
            prompt = "New disk PRG entry / loader name (blank keeps current, * clears, $ dir): "
            stdscr.addnstr(h-1, 0, prompt, w-1)
            stdscr.clrtoeol(); stdscr.refresh()
            entry = stdscr.getstr(h-1, len(prompt), 60).decode(errors="replace").strip()
            curses.noecho(); curses.curs_set(0)
            if title:
                rows[pos]["title"] = title
            if mode:
                if mode in ("prg", "crt", "disk"):
                    rows[pos]["mode"] = mode
                elif mode in ("d64", "d71", "d81"):
                    rows[pos]["mode"] = "disk"
                else:
                    msg = f"Ignored invalid launch mode: {mode}"
            if entry == "$":
                picked = show_disk_directory(stdscr, host, rows[pos], allow_select=True)
                if picked:
                    rows[pos]["entry"] = picked
                    msg = f"Selected entry {picked!r}"
            elif entry == "*":
                rows[pos]["entry"] = ""
            elif entry:
                rows[pos]["entry"] = entry
            rows[pos]["payload"] = payload_for(rows[pos]["mode"], rows[pos]["path"], rows[pos].get("entry", ""))
            commit_rows(base_rows, db_path)
            msg = "Updated title/mode/entry"
        elif ch == ord('q'):
            if sql_view_name == "A64 inbox":
                rows = base_rows
                sql_view_name = "All loaded rows"
                sql_filter_keys = None
                sql_filter_rank = None
                top = 0
                pos = 0
                msg = "Returned from A64 inbox to curator"
                continue
            return "Exited curator"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inventory", nargs="?", default="curator.db")
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--log", default="u2_curate_tui.log")
    ap.add_argument("--include-unsupported", action="store_true", help="show G64/TAP rows too")
    args = ap.parse_args()
    host = discover_ultimate() if args.ultimate == "auto" else args.ultimate
    if not host:
        raise SystemExit(1)
    nfc_ok, nfc_info = ensure_nfc_reader_connected()
    startup_nfc_msg = f"NFC reader ready: {nfc_info}" if nfc_ok else f"NFC reader unavailable: {nfc_info}"
    print(startup_nfc_msg)
    rows = read_csv(args.inventory)
    if not rows:
        print(f"No image rows found in {args.inventory}.")
        ans = input("Run inventory scan now to populate it? [Y/n] ").strip().lower()
        if ans in ("", "y", "yes"):
            inv = Path(__file__).with_name("u2_inventory.py")
            cmd = [sys.executable, str(inv), "--ultimate", args.ultimate, "--out", args.inventory]
            subprocess.check_call(cmd)
            rows = read_csv(args.inventory)
        if not rows:
            raise SystemExit(f"No rows in {args.inventory}; run ./u2_inventory.py first.")

    before_deleted = len(rows)
    rows = [r for r in rows if str(r.get("storage_status") or "present").lower() != "deleted"]
    hidden_deleted = before_deleted - len(rows)
    if hidden_deleted:
        print(f"Hidden {hidden_deleted} deleted row(s). Use SQL views for audit/recovery.")

    if not args.include_unsupported:
        before = len(rows)
        rows = [r for r in rows if is_supported_row(r)]
        skipped = before - len(rows)
        if skipped:
            print(f"Filtered out {skipped} unsupported G64/TAP row(s). Use --include-unsupported to show them.")
    if not rows:
        raise SystemExit(f"No supported rows in {args.inventory}")
    rows.sort(key=row_sort_key)
    result = curses.wrapper(run, rows, host, args.inventory, args.log, startup_nfc_msg)
    print(result)


if __name__ == "__main__":
    main()
