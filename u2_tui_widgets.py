#!/usr/bin/env python3
"""Reusable curses widgets for the curator TUI."""

from __future__ import annotations

import curses
from pathlib import Path

SAFE_BYTE_DOT = "⋅"  # U+22C5 DOT OPERATOR


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


def safe_byte_view_lines(data: bytes, width: int):
    lines = []
    cur = ""
    for b in data:
        if b in (9, 10, 13):
            ch = "\n" if b == 10 else "\t"
        elif 32 <= b <= 126:
            ch = chr(b)
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
