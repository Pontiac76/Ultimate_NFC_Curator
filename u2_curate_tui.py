#!/usr/bin/env python3
"""Rough curses curator: browse inventory, test launch, approve, export approved CSV."""

import argparse
import contextlib
import curses
import io
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from u2_common import *

FIELDS = ["title", "payload", "mode", "path", "entry", "machine_mode", "file_type", "detail", "status", "notes"]
UNSUPPORTED_EXTS = (".g64", ".tap")
UNSUPPORTED_TYPES = {"g64", "tap"}


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


def save_approved(rows, out):
    approved = [r for r in rows if r.get("status") == "approved"]
    write_csv(out, approved, FIELDS)
    return len(approved)


def save_state(rows, state_path):
    write_csv(state_path, rows, FIELDS)
    return len(rows)


def archive_import_source(path):
    """Rename a one-time import source so it is not imported again."""
    p = Path(path)
    if not p.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = p.with_name(f"{p.name}.imported-{stamp}")
    n = 1
    while target.exists():
        target = p.with_name(f"{p.name}.imported-{stamp}.{n}")
        n += 1
    p.rename(target)
    return target


def merge_settings(rows, settings_path, force_approved=False):
    """If a prior TSV exists, import its status/settings.

    Match order:
      1. exact path
      2. exact payload

    Do NOT match by title: several games legitimately have duplicate titles
    for different image variants/disks, e.g. Sonic .d71/.d81/side A/side B.
    """
    if not settings_path or not Path(settings_path).exists():
        return 0
    approved = read_csv(settings_path)
    by_path = {r.get("path", ""): r for r in approved if r.get("path")}
    by_payload = {r.get("payload", ""): r for r in approved if r.get("payload")}
    imported = 0
    for row in rows:
        match = None
        if row.get("path") in by_path:
            match = by_path[row.get("path")]
        elif row.get("payload") in by_payload:
            match = by_payload[row.get("payload")]
        if not match:
            continue
        # Do not copy title/path/payload between rows. Duplicate game titles and
        # variants are common; payload copying caused one approved Sonic image to
        # make every Sonic variant look approved on the next import.
        for key in ("mode", "entry", "machine_mode", "status", "notes"):
            if match.get(key):
                row[key] = match[key]
        # Recompute payload from this row's own path after importing mode/entry.
        if row.get("mode") and row.get("path"):
            row["payload"] = payload_for(row.get("mode", ""), row.get("path", ""), row.get("entry", ""))
        if force_approved:
            # Existing approved manifest means it should still show selected.
            row["status"] = match.get("status") or "approved"
        imported += 1
    return imported


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
        status, body = api_put(host, "/v1/machine:reset")
        print(f"reset HTTP {status}: {body.strip()}")
    elif cmd == "reboot":
        status, body = api_put(host, "/v1/machine:reboot")
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


def write_nfc_for_row(row):
    payload = nfc_payload_for_row(row)
    title = row.get("title", "")
    writer = Path(__file__).with_name("nfc_write_text.py")
    cmd = [sys.executable, str(writer), "--yes", payload]
    if os.geteuid() != 0:
        cmd.insert(0, "sudo")

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
            time.sleep(1.0)
            return True
        print("NFC write/verify FAILED. Retrying immediately; place/hold the card on the reader...")
        time.sleep(1.0)


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
    """Return (raw_listing, entries) for selected disk image using c1541."""
    path = row.get("path", "")
    lower = path.lower()
    if not lower.endswith((".d64", ".d71", ".d81")):
        raise RuntimeError("Selected item is not a supported disk image")
    suffix = Path(path).suffix or ".d64"
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


def show_help(stdscr):
    lines = [
        "Ultimate2+ NFC Curator Help",
        "",
        "Navigation:",
        "  Up/Down arrows  Move selection",
        "  PgUp/PgDn       Move one page",
        "  Home/End        Jump first/last item",
        "  /                Plain-text search/filter; blank clears",
        "  $                Show selected disk directory",
        "",
        "Game status/actions:",
        "  Space or a       Approve/unapprove selected game",
        "  t                Test launch selected game",
        "  T                Test launch, bypass prehelp",
        "  f                Mark failed",
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
        "  e                Edit launch mode/entry (* clears entry)",
        "  s                Save state and approved list",
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
    can_test = not issue
    return [
        ("Nav", [("↑/↓ move", True), ("PgUp/PgDn", True), ("Home/End", True), ("/ search", True), ("$ dir", is_disk), ("? help", True), ("q save+quit", True)]),
        ("Curate", [("Space/a approve", True), ("t test", can_test), ("T test no help", can_test), ("1 64/128", True), ("f failed", True), ("u unmark", True)]),
        ("NFC", [("w write", True), ("r read", True), ("m monitor", True)]),
        ("U2", [("M mount image", is_disk), ("D disk swap", False), ("G GO64/Y", True), ("B reboot", True), ("C console", True)]),
        ("Edit", [("k script", True), ("e entry", True), ("s save", True)]),
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


def run(stdscr, rows, host, out, log_path, state_path):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_WHITE, -1)
    status_rows, status_err = refresh_status_rows(host)
    issue_cache = {}
    pos = 0
    top = 0
    msg = ""
    search_query = ""
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
    while True:
        visible = [i for i, r in enumerate(rows) if row_matches_search(r, search_query)]
        if visible and pos not in visible:
            pos = visible[0]
            top = 0
        current_row = rows[pos] if visible else {}
        current_issue = selected_issue(host, current_row, issue_cache) if visible else ""

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        title = "Ultimate2+ C64 curator"
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
        footer = f"{count_text}  Output: {out}"
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
        if not visible and ch not in (ord('?'), ord('/'), ord('q'), ord('s'), curses.KEY_UP, curses.KEY_DOWN):
            msg = "No selected row; clear or change search"
            continue
        if ch == ord('?'):
            show_help(stdscr)
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
        elif ch == ord('1'):
            cur = "c128" if rows[pos].get("machine_mode", "c64") in ("c128", "128") else "c64"
            rows[pos]["machine_mode"] = "c64" if cur == "c128" else "c128"
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
                save_approved(rows, out)
                save_state(rows, state_path)
                msg = f"Wrote NFC for {rows[pos].get('title','')} and saved state"
            else:
                msg = "NFC write cancelled"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('m'):
            curses.endwin()
            monitor = Path(__file__).with_name("u2_tag_monitor.py")
            cmd = [sys.executable, str(monitor), "--ultimate", "auto", "--state", state_path, "--state", out]
            if os.geteuid() != 0:
                cmd.insert(0, "sudo")
            subprocess.call(cmd)
            msg = "Returned from NFC monitor"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('r'):
            curses.endwin()
            monitor = Path(__file__).with_name("u2_tag_monitor.py")
            cmd = [sys.executable, str(monitor), "--ultimate", "auto", "--state", state_path, "--state", out, "--no-launch"]
            if os.geteuid() != 0:
                cmd.insert(0, "sudo")
            subprocess.call(cmd)
            msg = "Returned from NFC read-only monitor"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('f'):
            rows[pos]["status"] = "failed"
        elif ch == ord('u'):
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
                status, body = api_put(host, "/v1/machine:reboot")
                msg = f"Reboot requested HTTP {status}: {body.strip()}"
            except Exception as e:
                msg = f"Reboot failed: {e}"
            status_rows, status_err = refresh_status_rows(host)
        elif ch == ord('M'):
            try:
                path = rows[pos].get("path", "")
                if not path.lower().endswith((".d64", ".d71", ".d81")):
                    msg = f"Mount skipped: not a supported disk image"
                else:
                    # mount_image may print USB0/USB1 fallback notes; capture them so
                    # they do not corrupt the curses screen.
                    captured = io.StringIO()
                    with contextlib.redirect_stdout(captured):
                        status, body = mount_image(host, path, "a")
                    msg = f"Mounted selected image to A/8"
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
            n = save_approved(rows, out)
            save_state(rows, state_path)
            msg = f"Saved {n} approved rows to {out}; state to {state_path}"
        elif ch in (ord('t'), ord('T')):
            issue = selected_issue(host, rows[pos], issue_cache)
            if issue:
                msg = issue
                continue
            # Keep curses clean: capture noisy helper output and append it to a log.
            captured_out = io.StringIO()
            captured_err = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
                    target_mode = rows[pos].get("machine_mode", "c64") or "c64"
                    skip_prehelp = ch == ord('T')
                    status, body = launch_payload(host, rows[pos]["payload"], d64_as_prg_loader=True, target_mode=target_mode, skip_prehelp=skip_prehelp)
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
            # Highlight the exact field being edited in the detail panel.
            draw_detail_panel(stdscr, detail_y, w, rows[pos], highlight="mode")
            prompt = "New launch mode (prg/crt/disk; blank keeps current): "
            stdscr.addnstr(h-1, 0, prompt, w-1)
            stdscr.clrtoeol(); stdscr.refresh()
            mode = stdscr.getstr(h-1, len(prompt), 20).decode().strip()

            stdscr.move(h-1, 0); stdscr.clrtoeol()
            draw_detail_panel(stdscr, detail_y, w, rows[pos], highlight="entry")
            prompt = "New disk PRG entry / loader name (blank keeps current, * clears, $ dir): "
            stdscr.addnstr(h-1, 0, prompt, w-1)
            stdscr.clrtoeol(); stdscr.refresh()
            entry = stdscr.getstr(h-1, len(prompt), 60).decode().strip()
            curses.noecho(); curses.curs_set(0)
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
            msg = "Updated launch mode/entry"
        elif ch == ord('q'):
            n = save_approved(rows, out)
            save_state(rows, state_path)
            return f"Saved {n} approved rows to {out}; state to {state_path}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inventory", nargs="?", default="rest_key_game_candidates.tsv")
    ap.add_argument("--out", default="approved_games.csv")
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--log", default="u2_curate_tui.log")
    ap.add_argument("--state", default="curator_state.tsv", help="TSV preserving approved/failed/launched/skipped rows")
    ap.add_argument("--include-unsupported", action="store_true", help="show G64/TAP rows too")
    ap.add_argument("--import-approved", action="store_true", help="one-time import approved manifest even when curator state exists, then archive it")
    args = ap.parse_args()
    host = discover_ultimate() if args.ultimate == "auto" else args.ultimate
    if not host:
        raise SystemExit(1)
    rows = read_csv(args.inventory)
    if not rows:
        raise SystemExit(f"No rows in {args.inventory}")

    imported_state = merge_settings(rows, args.state)
    if imported_state:
        print(f"Imported {imported_state} prior state row(s) from {args.state}.")

    # Approved manifests are treated as one-time migration/import sources.
    # Normal persistence should come from curator_state.tsv; otherwise stale
    # generated entries can keep re-entering the curated working set.
    imported_approved = 0
    if args.import_approved or not imported_state:
        imported_approved = merge_settings(rows, args.out, force_approved=True)
        if imported_approved:
            print(f"Imported {imported_approved} approved/settings row(s) from {args.out}.")
            save_state(rows, args.state)
            archived = archive_import_source(args.out)
            if archived:
                print(f"Archived one-time import source to {archived}.")
    elif Path(args.out).exists():
        print(f"State file is active; skipping one-time approved import from {args.out}.")

    if not args.include_unsupported:
        before = len(rows)
        rows = [r for r in rows if is_supported_row(r)]
        skipped = before - len(rows)
        if skipped:
            print(f"Filtered out {skipped} unsupported G64/TAP row(s). Use --include-unsupported to show them.")
    if not rows:
        raise SystemExit(f"No supported rows in {args.inventory}")
    rows.sort(key=row_sort_key)
    result = curses.wrapper(run, rows, host, args.out, args.log, args.state)
    print(result)


if __name__ == "__main__":
    main()
