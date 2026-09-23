#!/usr/bin/env python3
"""Shared rough-draft helpers for the Ultimate-II+/NFC card project."""

import csv
import ftplib
import ipaddress
import json
import os
import socket
import sqlite3
import subprocess
import shutil
import tempfile
import time
import shlex
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

STATE_FILE = Path(".u2_state.json")
HOSTS_FILE = Path("u2_hosts.txt")
DEFAULT_ROOT = "/"


def is_ignored_iface(name):
    return name.startswith(("docker", "br-", "veth", "lo"))


def get_lan_networks():
    """Return non-docker IPv4 networks from `ip -j addr`. Prefer /24-capable LANs."""
    try:
        data = json.loads(subprocess.check_output(["ip", "-j", "-4", "addr", "show", "scope", "global"]))
    except Exception:
        return []
    nets = []
    for iface in data:
        if is_ignored_iface(iface.get("ifname", "")):
            continue
        for a in iface.get("addr_info", []):
            ip = a.get("local")
            prefix = a.get("prefixlen")
            if ip and prefix is not None:
                nets.append(ipaddress.ip_interface(f"{ip}/{prefix}").network)
    return nets


def http_get(host, path="/v1/version", timeout=2):
    with urllib.request.urlopen(f"http://{host}{path}", timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def looks_like_ultimate(host, timeout=2):
    """Conservative-ish check: HTTP API and/or FTP root looks like Ultimate."""
    try:
        status, body = http_get(host, "/v1/version", timeout)
        if status == 200 and "version" in body and "errors" in body:
            return True
    except Exception:
        pass
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, 21, timeout=timeout)
        banner = ftp.getwelcome() or ""
        ftp.login("anonymous", "ftp@example.com")
        names = ftp.nlst()
        ftp.quit()
        if "Ultimate" in banner or {"Usb0", "Flash", "Temp"}.intersection(names):
            return True
    except Exception:
        pass
    try:
        status, body = http_get(host, "/", timeout)
        if status == 200 and "Ultimate" in body:
            return True
    except Exception:
        pass
    return False


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(host=None, last_usb=None):
    state = load_state()
    if host is not None:
        state["last_ip"] = host
        state["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if last_usb in (0, 1, "0", "1"):
        state["last_usb"] = int(last_usb)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def load_state_host():
    return load_state().get("last_ip")


def load_state_usb():
    usb = load_state().get("last_usb", 0)
    return int(usb) if str(usb) in ("0", "1") else 0


def neighbor_ultimate_hosts():
    """Return likely Ultimate hosts from ARP/neighbor table.

    The observed U2+ MAC here starts with 02:15:41. This avoids scanning huge
    /16 networks while still recovering after DHCP/network changes.
    """
    out = []
    try:
        text = subprocess.check_output(["ip", "neigh", "show"], text=True)
    except Exception:
        return out
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip = parts[0]
        lower = line.lower()
        if "02:15:41" in lower:
            out.append(ip)
    return out


def fallback_hosts():
    if not HOSTS_FILE.exists():
        HOSTS_FILE.write_text("# One host/IP per line, tried after last-known and LAN scan\n10.10.10.88\n192.168.1.88\n192.168.0.88\n")
    out = []
    for line in HOSTS_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def tcp_open(host, port, timeout=0.25):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((str(host), port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def resolve_ultimate(host="auto", verbose=True):
    """Resolve an Ultimate host argument.

    Use last-known/discovery when host is auto/blank/None, otherwise return host.
    """
    if host in (None, "", "auto"):
        found = discover_ultimate(verbose=verbose)
        if not found:
            raise RuntimeError("Could not find Ultimate-II+")
        return found
    return host


def discover_ultimate(verbose=True):
    tried = []
    last = load_state_host()
    if last:
        tried.append(("last", last))
        if verbose:
            print(f"Trying last known Ultimate: {last}")
        if looks_like_ultimate(last):
            save_state(last)
            return last

    for host in neighbor_ultimate_hosts():
        tried.append(("neighbor", host))
        if verbose:
            print(f"Trying neighbor-table Ultimate candidate: {host}")
        if looks_like_ultimate(host, timeout=2):
            save_state(host)
            return host

    for net in get_lan_networks():
        if net.prefixlen != 24:
            if verbose:
                print(f"Skipping non-/24 network {net}; use u2_hosts.txt fallback if needed")
            continue
        if verbose:
            print(f"Scanning LAN {net} for Ultimate HTTP/FTP...")
        hosts = [str(ip) for ip in net.hosts()]
        candidates = []
        def probe(h):
            return h if (tcp_open(h, 80, timeout=0.18) or tcp_open(h, 21, timeout=0.18)) else None
        with ThreadPoolExecutor(max_workers=96) as ex:
            futs = [ex.submit(probe, h) for h in hosts]
            for fut in as_completed(futs):
                h = fut.result()
                if h:
                    candidates.append(h)
        for host in sorted(candidates, key=lambda x: tuple(int(p) for p in x.split('.') if p.isdigit())):
            tried.append(("scan", host))
            if looks_like_ultimate(host):
                if verbose:
                    print(f"Found Ultimate at {host}")
                save_state(host)
                return host

    for host in fallback_hosts():
        tried.append(("fallback", host))
        if verbose:
            print(f"Trying fallback Ultimate host: {host}")
        if looks_like_ultimate(host, timeout=3):
            save_state(host)
            return host

    if verbose:
        print("Could not find Ultimate-II+; tried:")
        for kind, host in tried:
            print(f"  {kind}: {host}")
    return None


def strip_usb_prefix(path):
    if path.startswith("/Usb0/") or path.startswith("/Usb1/"):
        return path[5:]
    if path in ("/Usb0", "/Usb1"):
        return "/"
    return path if path.startswith("/") else "/" + path


def candidate_usb_paths(path):
    """Return concrete /Usb0 or /Usb1 paths. Tags may omit /UsbX."""
    if path.startswith("/Usb0/") or path == "/Usb0":
        tail = strip_usb_prefix(path)
        return ["/Usb0" + ("" if tail == "/" else tail), "/Usb1" + ("" if tail == "/" else tail)]
    if path.startswith("/Usb1/") or path == "/Usb1":
        tail = strip_usb_prefix(path)
        return ["/Usb1" + ("" if tail == "/" else tail), "/Usb0" + ("" if tail == "/" else tail)]
    tail = strip_usb_prefix(path)
    first = load_state_usb()
    second = 1 - first
    return [f"/Usb{first}" + tail, f"/Usb{second}" + tail]


def ftp_url(host, ultimate_path):
    return f"ftp://{host}" + urllib.parse.quote(ultimate_path, safe="/")


def note_usb_success(requested, used):
    if used.startswith("/Usb0"):
        save_state(last_usb=0)
    elif used.startswith("/Usb1"):
        save_state(last_usb=1)
    if used != requested and os.environ.get("U2_VERBOSE_USB_FALLBACK"):
        print(f"Note: {requested} failed; using {used}")


def ftp_download(host, ultimate_path):
    last_err = None
    for path in candidate_usb_paths(ultimate_path):
        try:
            with urllib.request.urlopen(ftp_url(host, path), timeout=20) as r:
                note_usb_success(ultimate_path, path)
                return r.read()
        except Exception as e:
            last_err = e
    raise last_err


def ftp_size(host, ultimate_path):
    """Return size for a file on Ultimate FTP, trying Usb0/Usb1 fallback."""
    last_err = None
    for path in candidate_usb_paths(ultimate_path):
        ftp = ftplib.FTP(host, timeout=5)
        try:
            ftp.login("anonymous", "ftp@example.com")
            size = ftp.size(path)
            ftp.quit()
            note_usb_success(ultimate_path, path)
            return size
        except Exception as e:
            last_err = e
            try:
                ftp.quit()
            except Exception:
                pass
    raise last_err


def ftp_list(host, ultimate_path):
    last_err = None
    for path in candidate_usb_paths(ultimate_path):
        ftp = ftplib.FTP(host, timeout=10)
        try:
            ftp.login("anonymous", "ftp@example.com")
            lines = []
            ftp.retrlines("LIST " + path, lines.append)
            ftp.quit()
            note_usb_success(ultimate_path, path)
            return lines
        except Exception as e:
            last_err = e
            try:
                ftp.quit()
            except Exception:
                pass
    raise last_err


def clean_temp(host):
    """Delete files from Ultimate /Temp via FTP. Best-effort; returns count deleted."""
    ftp = ftplib.FTP(host, timeout=10)
    deleted = 0
    try:
        ftp.login("anonymous", "ftp@example.com")
        ftp.cwd("/Temp")
        try:
            names = [n for n in ftp.nlst() if n not in (".", "..")]
        except Exception:
            names = []
        for name in names:
            try:
                ftp.delete(name)
                print(f"STEP clean temp deleted /Temp/{name}")
                deleted += 1
            except Exception as e:
                print(f"STEP clean temp warning /Temp/{name}: {e}")
        if not names:
            print("STEP clean temp: /Temp is empty")
        return deleted
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def parse_ftp_list(lines):
    rows = []
    for line in lines:
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            continue
        typ = "dir" if parts[0].startswith("d") else "file"
        size = int(parts[4]) if parts[4].isdigit() else 0
        rows.append({"type": typ, "size": size, "name": parts[8], "raw": line})
    return rows


def c1541_available():
    return shutil.which("c1541") is not None


def c1541_directory_from_file(image_file):
    if not c1541_available():
        raise RuntimeError("c1541 is not installed; run scripts/setup-pi.sh or install VICE")
    r = subprocess.run(["c1541", str(image_file), "-list"], text=True, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "c1541 -list failed").strip())
    entries = []
    disk_name = ""
    disk_id = ""
    blocks_free = None
    for line in r.stdout.splitlines():
        m = re.match(r'\s*0\s+"([^"]*)"\s*(.*)$', line)
        if m:
            disk_name = m.group(1).strip()
            disk_id = m.group(2).strip()
            continue
        m = re.match(r'\s*(\d+)\s+"([^"]+)"\s+(\S+)', line)
        if m:
            entries.append({"blocks": int(m.group(1)), "name": m.group(2).strip(), "type": m.group(3).upper()})
            continue
        m = re.match(r'\s*(\d+)\s+blocks free\.', line, re.I)
        if m:
            blocks_free = int(m.group(1))
    return {"disk_name": disk_name, "disk_id": disk_id, "blocks_free": blocks_free, "entries": entries, "raw": r.stdout}


def c1541_directory(img, suffix=".d64"):
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        Path(tmp).write_bytes(img)
        return c1541_directory_from_file(tmp)
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def extract_prg_with_c1541(img, wanted_name, suffix=".d64"):
    fd, image_tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    out_fd, out_tmp = tempfile.mkstemp(suffix=".prg")
    os.close(out_fd)
    os.remove(out_tmp)
    try:
        Path(image_tmp).write_bytes(img)
        r = subprocess.run(["c1541", image_tmp, "-read", wanted_name, out_tmp], text=True, capture_output=True, timeout=60)
        if r.returncode != 0 or not Path(out_tmp).exists():
            raise RuntimeError((r.stderr or r.stdout or f"c1541 could not read {wanted_name!r}").strip())
        return wanted_name, Path(out_tmp).read_bytes()
    finally:
        for p in (image_tmp, out_tmp):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass


def title_from_path(path):
    base = os.path.basename(path)
    for ext in (".d64", ".d71", ".d81", ".g64", ".prg", ".crt", ".tap"):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
    return base.replace("_", " ").replace("-", " ").strip()


def payload_for(mode, path, entry=""):
    # NFC tags intentionally omit /Usb0 or /Usb1; runtime tries last-known USB port first.
    return f"U2+:{mode}:{strip_usb_prefix(path)}" + (f"#{entry}" if entry else "")


def post_runner(host, endpoint, body):
    req = urllib.request.Request(f"http://{host}{endpoint}", data=body, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read().decode("utf-8", "replace")


def api_get_bytes(host, endpoint, params=None):
    url = f"http://{host}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read()


def api_get_json(host, endpoint, params=None):
    return json.loads(api_get_bytes(host, endpoint, params).decode("utf-8", "replace"))


def api_put(host, endpoint, params=None):
    url = f"http://{host}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {body}")


def api_post_bytes(host, endpoint, params, data):
    url = f"http://{host}{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode("utf-8", "replace")


def unmount_image(host, drive="a"):
    return api_put(host, f"/v1/drives/{drive}:remove")


def clear_cartridge(host):
    """Clear the configured cartridge/CRT slot so reset/reboot returns to a plain machine.

    U2+ run_crt can leave a cartridge configured across resets. The config API
    uses a per-setting endpoint with a value parameter.
    """
    return api_put(host, "/v1/configs/C64%20and%20Cartridge%20Settings/Cartridge", {"value": ""})


def reboot_machine(host, clear_cart=True):
    if clear_cart:
        clear_cartridge(host)
    return api_put(host, "/v1/machine:reboot")


def reset_machine(host, clear_cart=True):
    if clear_cart:
        clear_cartridge(host)
    return api_put(host, "/v1/machine:reset")


def cold_boot(host):
    return reset_machine(host)


def settle_with_blank_disk_then_boot(host, drive="a", blank_path="/blank.d64"):
    """Mount a known blank 1541 image, wait, then reset/cold boot.

    This gives the U2 a simple/known disk state before the machine reboots.
    If the blank image is missing, warn and reset anyway.
    """
    try:
        print(f"STEP mount blank disk before reset: {blank_path}")
        status, body = mount_image(host, blank_path, drive)
        print(f"STEP mounted blank: HTTP {status} {body.strip()}")
        print("STEP wait 2s for U2 settle")
        time.sleep(2.0)
    except Exception as e:
        print(f"STEP blank disk warning: {e}")
    print("STEP cold boot/reset")
    return cold_boot(host)


def screen_code_char(c, lowercase_charset=False):
    c &= 0x7f
    if c == 0x00:
        return "@"
    if 1 <= c <= 26:
        base = ord("a") if lowercase_charset else ord("A")
        return chr(base + c - 1)
    if c == 32:
        return " "
    if 33 <= c <= 63:
        return chr(c)
    if 65 <= c <= 90:
        base = ord("A") if lowercase_charset else ord("a")
        return chr(base + c - 65)
    if 97 <= c <= 122:
        base = ord("a") if lowercase_charset else ord("A")
        return chr(base + c - 97)
    return "?"


def read_text_screen(host):
    """Read current 40x25 VIC text screen and return plain text."""
    d018 = api_get_bytes(host, "/v1/machine:readmem", {"address": "d018", "length": "1"})[0]
    dd00 = api_get_bytes(host, "/v1/machine:readmem", {"address": "dd00", "length": "1"})[0]
    vic_bank = (3 - (dd00 & 0x03)) * 0x4000
    screen_base = vic_bank + ((d018 >> 4) & 0x0f) * 0x0400
    lowercase_charset = bool(d018 & 0x02)
    screen = api_get_bytes(host, "/v1/machine:readmem", {"address": f"{screen_base:04x}", "length": "1000"})
    lines = []
    for y in range(25):
        line = "".join(screen_code_char(screen[y * 40 + x], lowercase_charset) for x in range(40))
        lines.append(line)
    return "\n".join(lines), {"d018": d018, "dd00": dd00, "screen_base": screen_base, "lowercase_charset": lowercase_charset}


def normalize_screen_search_text(text):
    """General screen search normalization.

    Case-insensitive, and treats punctuation/box chars/dashes as separators so
    e.g. 'hit any key' can match 'hit─any─key'.
    """
    text = text.lower()
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


def wait_for_screen_text(host, needle, timeout=10.0, interval=0.25, exact=False):
    """Poll text screen until needle appears.

    exact=False: generalized search; punctuation/box chars become spaces.
    exact=True: case-insensitive exact substring search against raw screen text.
    """
    deadline = time.time() + timeout
    needle_cmp = needle.lower() if exact else normalize_screen_search_text(needle)
    mode = "exact" if exact else "general"
    while time.time() < deadline:
        try:
            text, info = read_text_screen(host)
            haystack = text.lower() if exact else normalize_screen_search_text(text)
            if needle_cmp in haystack:
                print(f"STEP screen wait matched {needle!r} mode={mode} at screen=${info['screen_base']:04x}")
                return True
        except Exception as e:
            print(f"STEP screen wait read warning: {e}")
        time.sleep(interval)
    print(f"STEP screen wait timed out for {needle!r} mode={mode}")
    return False


def wait_for_screen_row_text(host, needle, row, timeout=10.0, interval=0.5, screen_base=0x0400):
    """Very lightweight wait: read one 40-column screen row and search it."""
    deadline = time.time() + timeout
    row = max(0, min(24, int(row)))
    address = screen_base + row * 40
    needle = needle.upper()
    while time.time() < deadline:
        try:
            data = api_get_bytes(host, "/v1/machine:readmem", {"address": f"{address:04x}", "length": "40"})
            line = "".join(screen_code_char(b, False) for b in data).upper()
            if needle in line:
                print(f"STEP lightweight screen row matched {needle!r} at screen=${screen_base:04x} row={row}")
                return True
        except Exception as e:
            print(f"STEP lightweight row read warning: {e}")
        time.sleep(interval)
    print(f"STEP lightweight row wait timed out for {needle!r} row={row} screen=${screen_base:04x}")
    return False


def wait_for_basic_ready_region(host, timeout=180.0, interval=2.0, screen_base=0x0400, start_row=10, end_row=10):
    """Lightweight C64 BASIC READY wait after LOAD.

    By default, check only the expected READY row after SEARCHING/LOADING.
    """
    if start_row == end_row:
        return wait_for_screen_row_text(host, "READY", start_row, timeout=timeout, interval=interval, screen_base=screen_base)
    deadline = time.time() + timeout
    start = max(0, start_row) * 40
    rows = max(1, min(24, end_row) - max(0, start_row) + 1)
    length = rows * 40
    while time.time() < deadline:
        try:
            data = api_get_bytes(host, "/v1/machine:readmem", {"address": f"{screen_base + start:04x}", "length": str(length)})
            for rel_y in range(rows):
                line = "".join(screen_code_char(data[rel_y * 40 + x], False) for x in range(40)).upper()
                if "READY" in line:
                    print(f"STEP lightweight READY matched at screen=${screen_base:04x} row={start_row + rel_y}")
                    return True
        except Exception as e:
            print(f"STEP lightweight READY read warning: {e}")
        time.sleep(interval)
    print(f"STEP lightweight READY timed out rows={start_row}-{end_row} screen=${screen_base:04x}")
    return False


def detect_machine_mode(host):
    """Return c128/c64/unknown using ROM-ish memory reads after reset.

    C128 mode exposes BASIC 7 strings around $C000, including DIRECTORY/DLOAD.
    In C64 mode that region is normally RAM or different content.
    """
    import hashlib
    c000 = api_get_bytes(host, "/v1/machine:readmem", {"address": "c000", "length": "8192"})
    e000 = api_get_bytes(host, "/v1/machine:readmem", {"address": "e000", "length": "8192"})
    md5_c000 = hashlib.md5(c000).hexdigest()
    md5_e000 = hashlib.md5(e000).hexdigest()
    if b"DIRECTORY" in c000 and b"DLOAD" in c000:
        mode = "c128"
    elif b"COMMODORE 64" in c000 or b"BASIC BYTES FREE" in c000:
        mode = "c64"
    else:
        mode = "unknown"
    return {"mode": mode, "md5_c000": md5_c000, "md5_e000": md5_e000}


def mount_image(host, image_path, drive="a"):
    last_err = None
    for concrete in candidate_usb_paths(image_path):
        try:
            status, body = api_put(host, f"/v1/drives/{drive}:mount", {"image": concrete})
            note_usb_success(image_path, concrete)
            return status, body
        except Exception as e:
            last_err = e
    raise last_err


def inverse_ascii_case(text):
    # The C128/C64 keyboard buffer path wants the opposite ASCII case for alpha
    # keys to produce the intended shifted/unshifted result on the machine.
    return "".join(ch.lower() if ch.isupper() else ch.upper() if ch.islower() else ch for ch in text)


def keyboard_buffer_addrs(machine_mode):
    mode = "c128" if str(machine_mode).lower() in ("128", "c128") else "c64"
    return mode, *(('034a', '00d0') if mode == 'c128' else ('0277', '00c6'))


def wait_keyboard_buffer_empty(host, machine_mode, timeout=2.0, interval=0.02):
    mode, _buf_addr, count_addr = keyboard_buffer_addrs(machine_mode)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            count = api_get_bytes(host, "/v1/machine:readmem", {"address": count_addr, "length": "1"})[0]
            if count == 0:
                return True
        except Exception as e:
            print(f"TYPE buffer drain warning mode={mode}: {e}")
            return False
        time.sleep(interval)
    print(f"TYPE buffer drain timed out mode={mode} count=${count_addr}")
    return False


def inject_key_chunk(host, text, machine_mode="c128", invert_case=True):
    """Inject one already-sized chunk through the keyboard buffer."""
    wire_text = inverse_ascii_case(text) if invert_case else text
    mode, buf_addr, count_addr = keyboard_buffer_addrs(machine_mode)
    print(f"TYPE mode={mode} buffer=${buf_addr} count=${count_addr} intended={text!r} wire={wire_text!r}")
    data = wire_text.encode("ascii")
    # On this U2+ the reliable memory-write route is PUT with hex data.
    # POST /v1/machine:writemem can return 404 depending on firmware/state.
    api_put(host, "/v1/machine:writemem", {"address": buf_addr, "data": data.hex()})
    result = api_put(host, "/v1/machine:writemem", {"address": count_addr, "data": f"{len(data):02x}"})
    wait_keyboard_buffer_empty(host, machine_mode)
    return result


def inject_keys(host, text, machine_mode="c128", invert_case=True, chunk_size=10, chunk_delay=0.05):
    """Inject arbitrary text through the keyboard buffer, safely chunked.

    C128 native mode uses buffer $034A/count $00D0.
    C64 mode uses buffer $0277/count $00C6.
    """
    last = None
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        last = inject_key_chunk(host, chunk, machine_mode=machine_mode, invert_case=invert_case)
        if i + chunk_size < len(text) and chunk_delay:
            print(f"TYPE chunk delay {chunk_delay}s")
            time.sleep(chunk_delay)
    return last


def inject_c128_keys(host, text, invert_case=True):
    return inject_keys(host, text, machine_mode="c128", invert_case=invert_case)


def d64_single_prg(host, image_path):
    """Return (name, prg_bytes) if a disk image has exactly one directory entry and it is a PRG."""
    if not image_path.lower().endswith((".d64", ".d71", ".d81")):
        return None
    img = ftp_download(host, image_path)
    suffix = Path(image_path).suffix.lower() or ".d64"
    info = c1541_directory(img, suffix=suffix)
    entries = [f for f in info["entries"] if f.get("name")]
    print(f"STEP disk directory entries: {[(e['name'], e['type']) for e in entries]}")
    if len(entries) == 1 and entries[0]["type"] == "PRG":
        return extract_prg_with_c1541(img, entries[0]["name"], suffix=suffix)
    return None


def script_path_for_image(image_path, script_dir="scripts"):
    base = os.path.basename(strip_usb_prefix(image_path)) or "image"
    # Keep the filename recognizable, but remove path-hostile characters.
    base = re.sub(r"[\\/]+", "_", base)
    return Path(script_dir) / f"{base}.txt"


def send_script_keys(host, text, machine_mode, chunk_size=10, chunk_delay=1.0):
    text = text.replace("/n", "\r").replace("/r", "\r")
    return inject_keys(host, text, machine_mode=machine_mode, chunk_size=chunk_size, chunk_delay=chunk_delay)


SCRIPT_HELP_HEADER = """# Ultimate NFC launch script help
# Commands before a prehelp marker are post-launch commands:
#   sleep 15
#   delay 1
#   waitfortext READY 10
#   waitforexacttext READY 10
#   sendkey some text/n        (/n means Enter)
#
# Pre-run help for disk launches:
#   A standalone _ or prehelp switches the rest of THAT SECTION to literal
#   screen text. After that marker, # is displayed, not treated as a comment.
#   Use this near the end so comments/help below do not appear on screen.
#
# Prehelp control tags:
#   {CLR}      clear screen and fill text color with background color
#   {HOME}     move help renderer to row 0, column 0
#   {HIDE}     switch to current background color; also marks cursor target line
#
# Color tags:
#   {BLACK} {WHITE} {RED} {CYAN} {PURPLE} {GREEN} {BLUE} {YELLOW}
#   {ORANGE} {BROWN} {LTRED} {DKGRAY} {GRAY} {LTGREEN} {LTBLUE} {LTGRAY}
#
# Example:
#   _
#   {CLR}{WHITE}Game title
#   {LTRED}Important:{WHITE} Press {GREEN}Y{WHITE} if asked.
#   {LTBLUE}Press ENTER to start.
#   {HIDE}run

"""


def ensure_script_section(image_path, script_dir="scripts"):
    """Ensure script file exists and has a [path] section for this exact image."""
    path = script_path_for_image(image_path, script_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    image_key = strip_usb_prefix(image_path)
    if not path.exists():
        path.write_text(SCRIPT_HELP_HEADER)
    text = path.read_text()
    if "# Ultimate NFC launch script help" not in text:
        path.write_text(SCRIPT_HELP_HEADER + text)
        text = path.read_text()
    if f"[{image_key}]" not in text:
        with path.open("a") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write(f"\n[{image_key}]\n")
            f.write("# _\n")
            f.write("# {CLR}{WHITE}Instructions go here. {GREEN}Press ENTER to start.\n")
            f.write("# {HIDE}run\n")
            f.write("#\n")
            f.write("# sleep 15\n")
            f.write("# sendkey /n\n")
    return path


def script_lines_for_image(image_path, script_dir="scripts"):
    path = script_path_for_image(image_path, script_dir)
    if not path.exists():
        return None, []
    image_key = strip_usb_prefix(image_path)
    current = None
    lines = []
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            continue
        if current == image_key:
            lines.append((lineno, raw))
    return path, lines


def is_prehelp_marker(line):
    """A standalone marker that switches this section into prehelp mode."""
    stripped = line.strip().lower()
    return stripped in ("prehelp", "_", "_prehelp")


def strip_prehelp_tags(text):
    return re.sub(r"\{[A-Za-z0-9]+\}", "", text)


def lint_prehelp_text(text, first_lineno=1):
    """Return human-readable prehelp warnings/errors for tag sanity.

    first_lineno is the source-file line number corresponding to text line 1.
    Visible help may use lines 1-24; a {HIDE} launch line may live on line 25.
    """
    problems = []
    valid = set(SCREEN_COLORS) | {"CLR", "HOME", "HIDE"}
    logical_row = 0
    hide_seen = False
    for rel_lineno, line in enumerate(text.splitlines(), 1):
        lineno = first_lineno + rel_lineno - 1
        tags = [m.group(1).strip().upper() for m in re.finditer(r"\{([^}]*)\}", line)]
        has_hide = "HIDE" in tags
        if has_hide:
            hide_seen = True
        if logical_row >= 24 and not (logical_row == 24 and has_hide):
            problems.append(f"line {lineno}: prehelp exceeds screen; only lines 1-24 plus optional line 25 {{HIDE}} command fit")
        i = 0
        printable_cols = 0
        while i < len(line):
            ch = line[i]
            if ch == "{":
                end = line.find("}", i + 1)
                if end < 0:
                    problems.append(f"line {lineno}: unclosed tag starting at column {i+1}")
                    break
                name = line[i + 1:end].strip().upper()
                if not name:
                    problems.append(f"line {lineno}: empty tag at column {i+1}")
                elif not re.fullmatch(r"[A-Z0-9]+", name):
                    problems.append(f"line {lineno}: malformed tag {{{line[i+1:end]}}}")
                elif name not in valid:
                    problems.append(f"line {lineno}: unknown tag {{{line[i+1:end]}}}")
                i = end + 1
                continue
            if ch == "}":
                problems.append(f"line {lineno}: unmatched }} at column {i+1}")
            printable_cols += 1
            i += 1
        if printable_cols > 39:
            problems.append(f"line {lineno}: {printable_cols} printable chars; only first 39 will be shown")
        logical_row += 1
    if text.strip() and not hide_seen:
        problems.append("prehelp is missing required {HIDE} launch command line")
    return problems


LINTER_MARKER = "# Linter:"


def strip_linter_annotations(script_path):
    """Remove inline linter comments previously added for editor guidance."""
    path = Path(script_path)
    if not path.exists():
        return False
    old_lines = path.read_text().splitlines()
    new_lines = []
    changed = False
    for line in old_lines:
        if LINTER_MARKER in line:
            line = line.split(LINTER_MARKER, 1)[0].rstrip()
            changed = True
        new_lines.append(line)
    if changed:
        path.write_text("\n".join(new_lines) + "\n")
    return changed


def annotate_linter_problems(script_path, problems):
    """Append '# Linter: ...' to source lines mentioned by lint output."""
    path = Path(script_path)
    if not path.exists():
        return False
    strip_linter_annotations(path)
    lines = path.read_text().splitlines()
    by_line = {}
    for p in problems:
        m = re.match(r"line (\d+):\s*(.*)", p)
        if not m:
            continue
        lineno = int(m.group(1))
        msg = m.group(2)
        by_line.setdefault(lineno, []).append(msg)
    if not by_line:
        return False
    for lineno, msgs in by_line.items():
        if 1 <= lineno <= len(lines):
            base = lines[lineno - 1].rstrip()
            pad = " " * max(1, 40 - len(base))
            lines[lineno - 1] = f"{base}{pad}{LINTER_MARKER} {'; '.join(msgs)}"
    path.write_text("\n".join(lines) + "\n")
    return True


def lint_script_file(script_path):
    """Lint all prehelp blocks in a script file."""
    path = Path(script_path)
    if not path.exists():
        return []
    current = None
    collecting = False
    buf = []
    buf_start_lineno = 1
    problems = []
    def finish():
        if collecting:
            section_problems = lint_prehelp_text("\n".join(buf), first_lineno=buf_start_lineno)
            if section_problems:
                problems.append(f"[{current or 'unknown'}]")
                problems.extend(section_problems)
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            finish()
            current = stripped[1:-1].strip()
            collecting = False
            buf = []
            continue
        if not collecting and is_prehelp_marker(raw):
            collecting = True
            buf = []
            buf_start_lineno = lineno + 1
            continue
        if collecting:
            buf.append(raw)
    finish()
    return problems


def prehelp_text_for_image(image_path, script_dir="scripts"):
    """Return raw display text after the prehelp marker in an image section.

    Prehelp takes over launch responsibility. Anything before the marker is
    discarded, and anything after it is display text rather than post-script
    commands. Trailing blank lines are trimmed so they do not affect layout.
    """
    path, lines = script_lines_for_image(image_path, script_dir)
    if not path:
        return None, ""
    collecting = False
    out = []
    for lineno, raw in lines:
        stripped = raw.strip().lower()
        if not collecting:
            if is_prehelp_marker(raw):
                collecting = True
            continue
        if stripped == "endprehelp":
            continue
        # In prehelp mode, # is normal printable text, not a script comment.
        out.append(raw.rstrip("\n"))
    while out and not out[-1].strip():
        out.pop()
    text = "\n".join(out)
    return path, text


SCREEN_COLORS = {
    "BLACK": 0, "WHITE": 1, "RED": 2, "CYAN": 3,
    "PURPLE": 4, "GREEN": 5, "BLUE": 6, "YELLOW": 7,
    "ORANGE": 8, "BROWN": 9, "LTRED": 10, "LIGHTRED": 10,
    "DKGRAY": 11, "DARKGRAY": 11, "GRAY": 12, "GREY": 12,
    "LTGREEN": 13, "LIGHTGREEN": 13, "LTBLUE": 14, "LIGHTBLUE": 14,
    "LTGRAY": 15, "LIGHTGRAY": 15, "LTGREY": 15, "LIGHTGREY": 15,
}


def ascii_to_screen_code(ch, lowercase_charset=False):
    o = ord(ch)
    if ch == "\n":
        return None
    if ch == "@":
        return 0
    if lowercase_charset:
        # In the C64/C128 upper/lower character set, screen codes 1-26 are
        # lowercase and 65-90 are uppercase.
        if "A" <= ch <= "Z":
            return o
        if "a" <= ch <= "z":
            return o - 96
    else:
        # In the default upper/graphics set, both PC cases collapse to uppercase.
        if "A" <= ch <= "Z":
            return o - 64
        if "a" <= ch <= "z":
            return o - 96
    # Conservative printable screen-code set. Unsupported PC/Unicode
    # characters are converted to spaces by render_help_to_screen.
    if ch in " !\"#$%&'()*+,-./0123456789:;<=>?[]":
        return o
    return None


def write_mem(host, address, data, chunk_size=None):
    # Use PUT hex-data form. It supports multi-byte writes and is reliable on
    # this U2+ where POST /v1/machine:writemem may return 404. Keep chunks
    # small; larger writes have caused U2 HTTP hangs/timeouts and may perturb
    # sensitive loaders.
    if chunk_size is None:
        chunk_size = int(os.environ.get("U2_WRITEMEM_CHUNK", "40"))
    data = bytes(data)
    last = None
    for off in range(0, len(data), chunk_size):
        chunk = data[off:off + chunk_size]
        last = api_put(host, "/v1/machine:writemem", {
            "address": f"{address + off:04x}",
            "data": chunk.hex(),
        })
    return last


def get_cursor_blink(host):
    """Return True if BASIC editor cursor blink appears enabled ($CC == 0)."""
    return api_get_bytes(host, "/v1/machine:readmem", {"address": "00cc", "length": "1"})[0] == 0


def set_cursor_blink(host, enabled):
    """Set BASIC editor cursor blink. $CC nonzero suppresses cursor blink on C64/C128 40-col."""
    try:
        write_mem(host, 0x00cc, [0x00 if enabled else 0x01])
    except Exception as e:
        print(f"STEP cursor blink warning: {e}")


def send_cursor_controls(host, row, col, machine_mode):
    """Move visible editor cursor using control chars, chunked for keyboard buffer size."""
    controls = "\x13" + ("\x11" * row) + ("\x1d" * col)  # HOME, DOWN*, RIGHT*
    inject_keys(host, controls, machine_mode=machine_mode, invert_case=False, chunk_size=8, chunk_delay=0.1)


def unreverse_screen(host, screen_base):
    """Clear reverse-video/high-bit artifacts from the active 40x25 screen."""
    try:
        screen = api_get_bytes(host, "/v1/machine:readmem", {"address": f"{screen_base:04x}", "length": "1000"})
        cleaned = bytes(b & 0x7f for b in screen)
        write_mem(host, screen_base, cleaned)
        print("STEP prehelp cleared reverse-video screen artifacts")
    except Exception as e:
        print(f"STEP prehelp unreverse warning: {e}")


def prehelp_cells(help_text, hidden_color=6, lowercase_charset=False):
    """Parse prehelp text into (screen_buf, color_buf, last_hidden_cell, replacements).

    This is shared by real C64 rendering and terminal preview.
    """
    row = 0
    col = 0
    color = SCREEN_COLORS["WHITE"]
    cells = []
    first_hidden_cell = None
    last_hidden_cell = None
    replacements = []
    hide_active = False
    max_cols = 39
    token_re = re.compile(r"\{([A-Za-z0-9]+)\}")

    def clear_screen():
        nonlocal cells
        cells = []

    def emit_char(ch):
        nonlocal row, col, first_hidden_cell, last_hidden_cell
        if row >= 25:
            return
        if col >= max_cols:
            return
        code = ascii_to_screen_code(ch, lowercase_charset=lowercase_charset)
        if code is None:
            if not ch.isspace():
                replacements.append(ch)
            code = 0x20
        cells.append((row, col, code, color, ch, hide_active))
        if hide_active and not ch.isspace() and code != 0x20:
            if first_hidden_cell is None:
                first_hidden_cell = (row, col)
            last_hidden_cell = (row, col)
        col += 1

    for raw_line in help_text.splitlines():
        pos = 0
        if row >= 25:
            break
        for m in token_re.finditer(raw_line):
            for ch in raw_line[pos:m.start()]:
                emit_char(ch)
            name = m.group(1).upper()
            if name in SCREEN_COLORS:
                color = SCREEN_COLORS[name]
            elif name == "HIDE":
                hide_active = True
                color = hidden_color
            elif name == "CLR":
                clear_screen()
                row = 0
                col = 0
                hide_active = False
            elif name == "HOME":
                row = 0
                col = 0
            pos = m.end()
        for ch in raw_line[pos:]:
            emit_char(ch)
        row += 1
        col = 0
        hide_active = False

    screen_buf = [0x20] * 1000
    color_buf = [hidden_color] * 1000
    char_buf = [" "] * 1000
    hide_buf = [False] * 1000
    for y, x, code, c, ch, hidden in cells:
        if 0 <= y < 25 and 0 <= x < 40:
            off = y * 40 + x
            screen_buf[off] = code
            color_buf[off] = c
            char_buf[off] = ch
            hide_buf[off] = hidden
    return screen_buf, color_buf, char_buf, hide_buf, last_hidden_cell, replacements


def render_help_to_screen(host, help_text, marker="HELP", machine_mode="c64", auto_hidden_command=None):
    """Render color-marked prehelp directly to screen/color RAM.

    The script owns the hidden launch command, e.g. {HIDE}run.  {HIDE}
    switches to the current background color discovered from $D021.
    """
    # Keep Commodore charset/registers alone. Convert PC-side help text to
    # lowercase, which renders as uppercase in the normal C64 upper/graphics
    # character set. This avoids touching $D018 after a program has loaded.
    help_text = help_text.lower()
    lowercase_charset = False
    screen_text, info = read_text_screen(host)
    screen_base = info["screen_base"]
    hidden_color = api_get_bytes(host, "/v1/machine:readmem", {"address": "d021", "length": "1"})[0] & 0x0f
    upper = screen_text.upper()
    idx = upper.find(marker.upper())
    marker_row, marker_col = (22, 0) if idx < 0 else divmod(idx, 41)  # 40 chars plus newline in joined text
    if marker_row > 24:
        marker_row, marker_col = 22, 0
    print(f"STEP prehelp screen=${screen_base:04x} hidden_color={hidden_color} marker_row={marker_row} marker_col={marker_col}")

    screen_buf, color_buf, _char_buf, _hide_buf, last_hidden_cell, replacements = prehelp_cells(
        help_text,
        hidden_color=hidden_color,
        lowercase_charset=lowercase_charset,
    )

    if replacements:
        sample = "".join(dict.fromkeys(replacements[:20]))
        print(f"STEP prehelp warning: replaced {len(replacements)} unsupported character(s) with spaces: {sample!r}")

    pretarget = None
    if last_hidden_cell:
        cur_row, cur_col = last_hidden_cell
        pretarget = (cur_row, min(cur_col + 1, 39))
        try:
            print(f"STEP prehelp pre-position cursor row={pretarget[0]} col={pretarget[1]} before drawing")
            write_mem(host, 0x00d6, [pretarget[0]])
            write_mem(host, 0x00d3, [pretarget[1]])
            send_cursor_controls(host, pretarget[0], pretarget[1], machine_mode)
        except Exception as e:
            print(f"STEP prehelp pre-position warning: {e}")

    # Set colors and clear text first; finally reveal text one row at a time.
    write_mem(host, 0xd800, color_buf)
    write_mem(host, screen_base, [0x20] * 1000)
    prehelp_line_delay = float(os.environ.get("U2_PREHELP_LINE_DELAY", "0.5"))
    for y in range(25):
        row = screen_buf[y * 40:(y + 1) * 40]
        if any(ch != 0x20 for ch in row):
            write_mem(host, screen_base + y * 40, row)
            time.sleep(prehelp_line_delay)

    if not last_hidden_cell and auto_hidden_command:
        codes = [ascii_to_screen_code(c, lowercase_charset=lowercase_charset) or 0x20 for c in auto_hidden_command]
        off = marker_row * 40 + marker_col
        write_mem(host, screen_base + off, codes)
        write_mem(host, 0xd800 + off, [hidden_color] * len(codes))
        last_hidden_cell = (marker_row, marker_col + len(codes) - 1)
        print(f"STEP prehelp auto-hidden command {auto_hidden_command!r} at row={marker_row} col={marker_col}")

    if last_hidden_cell:
        # BASIC ENTER operates on the cursor's current screen line. Move the
        # editor cursor to the line where the hidden command was rendered.
        cur_row, cur_col = last_hidden_cell
        try:
            target_col = min(cur_col + 1, 39)
            write_mem(host, 0x00d6, [cur_row])
            write_mem(host, 0x00d3, [target_col])
            # The visible cursor was already moved before drawing. Avoid moving
            # it again across finished help text; just clean reverse artifacts.
            unreverse_screen(host, screen_base)
            print(f"STEP prehelp cursor moved to row={cur_row} col={target_col}")
        except Exception as e:
            print(f"STEP prehelp cursor warning: {e}")
    print("STEP prehelp displayed; user ENTER should submit hidden command")


def maybe_show_prelaunch_help(host, image_path, machine_mode, script_dir="scripts"):
    path, text = prehelp_text_for_image(image_path, script_dir)
    if not text:
        return False
    print(f"STEP prehelp found in {path}; rendering help now that LOAD reached READY")
    # No extra marker/key injection is needed here. boot_mount_load_run already
    # waited for the post-LOAD READY, so render help immediately and let the
    # script's {HIDE} line decide where Enter should run from.
    render_help_to_screen(host, text, marker="READY", machine_mode=machine_mode, auto_hidden_command=None)
    return True


def run_launch_script(host, image_path, machine_mode, script_dir="scripts"):
    path, lines = script_lines_for_image(image_path, script_dir)
    if not path or not lines:
        return False
    print(f"STEP running post-launch script {path} section [{strip_usb_prefix(image_path)}]")
    ran = False
    for lineno, raw in lines:
        if is_prehelp_marker(raw):
            print(f"STEP script {lineno}: prehelp marker found; remaining section is pre-run help, not post script")
            return ran
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError as e:
            print(f"STEP script {lineno}: ignored parse error: {e}")
            continue
        if not parts:
            continue
        cmd = parts[0].lower()
        if cmd in ("sleep", "delay"):
            try:
                seconds = float(parts[1]) if len(parts) > 1 else 1.0
            except ValueError:
                print(f"STEP script {lineno}: ignored bad sleep value")
                continue
            print(f"STEP script {lineno}: sleep {seconds}s")
            time.sleep(seconds)
            ran = True
        elif cmd in ("waitfor", "waittext", "waitscreen", "waitfortext", "waitforexacttext"):
            if len(parts) < 2:
                print(f"STEP script {lineno}: ignored {cmd} without text")
                continue
            text = parts[1]
            timeout = 15.0
            if len(parts) > 2:
                try:
                    timeout = float(parts[2])
                except ValueError:
                    pass
            exact = cmd == "waitforexacttext"
            print(f"STEP script {lineno}: {cmd} {text!r} timeout={timeout}s exact={exact}")
            wait_for_screen_text(host, text, timeout=timeout, exact=exact)
            ran = True
        elif cmd in ("sendkey", "sendkeys", "type"):
            if len(parts) < 2:
                print(f"STEP script {lineno}: ignored sendkey without text")
                continue
            text = " ".join(parts[1:])
            print(f"STEP script {lineno}: sendkey {text!r} mode={machine_mode}")
            send_script_keys(host, text, machine_mode)
            ran = True
        else:
            print(f"STEP script {lineno}: ignored unknown command {cmd!r}")
            continue
    return ran


def boot_mount_load_run(host, image_path, target_mode="c64", drive="a", entry="", skip_prehelp=False):
    """Cold boot, detect mode, optionally GO64, then mount/load/run a disk image.

    entry controls the LOAD target for disk mode. Blank means LOAD"*".
    skip_prehelp bypasses any pre-run help block and sends RUN normally.
    Disk mode must always use the mounted-disk LOAD path, even when the image
    contains a single PRG. Explicit prg mode is the only DMA/run_prg path.
    """
    target_mode = "c128" if str(target_mode).lower() in ("128", "c128") else "c64"
    print(f"STEP target_mode={target_mode} image={image_path} drive={drive}")

    try:
        print("STEP unmount image")
        unmount_image(host, drive)
    except Exception as e:
        print(f"STEP unmount warning: {e}")
    settle_with_blank_disk_then_boot(host, drive)
    print("STEP wait for BASIC READY after reset")
    if not wait_for_screen_text(host, "READY", timeout=12.0):
        print("STEP READY not detected; falling back to 3s reset delay")
        time.sleep(3.0)
    else:
        time.sleep(0.5)
    print("STEP read ROM / detect mode")
    detected = detect_machine_mode(host)
    print(f"STEP detected mode={detected['mode']} md5_c000={detected['md5_c000']} md5_e000={detected['md5_e000']}")

    if target_mode == "c128" and detected["mode"] == "c64":
        raise RuntimeError("Target is C128, but machine appears to be in C64 mode after cold boot")

    if target_mode == "c64" and detected["mode"] == "c128":
        print("STEP ensure BASIC READY before GO64")
        if not wait_for_screen_text(host, "READY", timeout=8.0):
            print("STEP READY not detected before GO64; proceeding anyway")
        print("STEP request C64 mode: go64")
        inject_keys(host, "go64\r", machine_mode="c128")
        print("STEP wait for GO64 confirmation prompt")
        if not wait_for_screen_text(host, "ARE YOU SURE", timeout=8.0):
            print("STEP prompt not detected; falling back to short delay before Y")
            time.sleep(1.0)
        print("STEP confirm C64 mode: y")
        inject_keys(host, "y\r", machine_mode="c128")
        print("STEP wait for C64 BASIC after GO64")
        if wait_for_screen_text(host, "COMMODORE 64 BASIC", timeout=8.0):
            print("STEP C64 BASIC banner detected")
            wait_for_screen_text(host, "READY", timeout=4.0)
        else:
            print("STEP C64 BASIC banner not detected; falling back to 3s settle delay")
            time.sleep(3.0)
        active_mode = "c64"
    else:
        active_mode = detected["mode"] if detected["mode"] in ("c64", "c128") else target_mode

    print("STEP mount image")
    status, body = mount_image(host, image_path, drive)
    print(f"STEP mounted image for {target_mode}: HTTP {status} {body.strip()}")

    load_target = entry or "*"
    print(f'STEP send LOAD command: lO"{load_target}",8,1')
    # C64/C128 keyboard buffers are small. LOAD"*",8,1 is exactly short
    # enough, but named loaders can exceed the safe buffer length and overwrite
    # adjacent editor state, causing odd colors/control behavior. Type long
    # commands in small chunks instead.
    send_script_keys(host, f'lO"{load_target}",8,1\r', active_mode, chunk_size=10, chunk_delay=0.2)
    prehelp_path, prehelp_text = prehelp_text_for_image(image_path)
    if prehelp_text and skip_prehelp:
        print("STEP prehelp present but skipped for this launch")
        prehelp_text = ""
    if prehelp_text:
        print("STEP prehelp pending; wait for LOAD to finish before typing HELP marker")
        ready_timeout = float(os.environ.get("U2_PREHELP_READY_TIMEOUT", "180"))
        ready_interval = float(os.environ.get("U2_PREHELP_READY_INTERVAL", "2.0"))
        ready_row = int(os.environ.get("U2_PREHELP_READY_ROW", "10"))
        if not wait_for_basic_ready_region(host, timeout=ready_timeout, interval=ready_interval, start_row=ready_row, end_row=ready_row):
            raise RuntimeError(
                "Prehelp aborted: LOAD did not reach READY before timeout; "
                "not rendering help while Ultimate/load may still be busy"
            )
        if maybe_show_prelaunch_help(host, image_path, active_mode):
            print("STEP prehelp active; user ENTER will run the game")
            print("STEP skipping post-launch script because launch is now user-controlled")
    else:
        print("STEP wait 2s for load")
        time.sleep(2.0)
        print("STEP send RUN")
        inject_keys(host, "run\r", machine_mode=active_mode)
        run_launch_script(host, image_path, active_mode)
    print("STEP launch sequence complete")
    return status, body


def launch_payload(host, payload, d64_as_prg_loader=True, target_mode="c64", skip_prehelp=False):
    if not payload.startswith("U2+:"):
        raise RuntimeError("Payload does not start with U2+:")
    mode, rest = payload[4:].split(":", 1)
    path, _, entry = rest.partition("#")
    lower = path.lower()
    if mode == "prg":
        blob = ftp_download(host, path)
        if lower.endswith((".d64", ".d71", ".d81")):
            name, blob = extract_prg_with_c1541(blob, entry, suffix=Path(path).suffix.lower() or ".d64")
            print(f"Extracted {name!r}, {len(blob)} bytes")
        if not blob:
            raise RuntimeError(f"Downloaded PRG payload is 0 bytes: {path}")
        result = post_runner(host, "/v1/runners:run_prg", blob)
        run_launch_script(host, path, target_mode)
        return result
    if mode == "crt":
        blob = ftp_download(host, path)
        if not blob:
            raise RuntimeError(f"Downloaded CRT payload is 0 bytes: {path}")
        return post_runner(host, "/v1/runners:run_crt", blob)
    if mode in ("d64", "disk"):
        image_exts = (".d64", ".d71", ".d81")
        if lower.endswith(image_exts):
            return boot_mount_load_run(host, path, target_mode=target_mode, drive="a", entry=entry, skip_prehelp=skip_prehelp)
        if lower.endswith((".g64", ".tap")):
            raise RuntimeError("G64/TAP launch is intentionally unsupported for now")
    raise RuntimeError(f"Unsupported launch mode {mode!r} for path {path!r}")


def is_sqlite_path(path):
    return str(path).lower().endswith((".db", ".sqlite", ".sqlite3"))


def sqlite_connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_lookup(conn, table, names=()):
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (pk_ID INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)")
    for name in names:
        conn.execute(f"INSERT OR IGNORE INTO {table} (name) VALUES (?)", (name,))


def ensure_file_type(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS FileType (
            pk_ID INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    existing = {r[1] for r in conn.execute("PRAGMA table_info(FileType)")}
    if "enabled" not in existing:
        conn.execute("ALTER TABLE FileType ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    for name, enabled in (("d64", 1), ("d71", 1), ("d81", 1), ("prg", 1), ("crt", 1), ("g64", 0), ("tap", 0)):
        conn.execute("INSERT OR IGNORE INTO FileType (name, enabled) VALUES (?, ?)", (name, enabled))
        if enabled == 0:
            conn.execute("UPDATE FileType SET enabled = 0 WHERE name = ?", (name,))


def lookup_id(conn, table, name):
    name = (name or "").strip()
    if not name:
        return None
    conn.execute(f"INSERT OR IGNORE INTO {table} (name) VALUES (?)", (name,))
    return conn.execute(f"SELECT pk_ID FROM {table} WHERE name = ?", (name,)).fetchone()[0]


def ensure_rows_table(conn, fields=None):
    ensure_lookup(conn, "Status", ["approved", "failed", "launched", "skipped"])
    ensure_lookup(conn, "StorageStatus", ["present", "missing", "deleted", "returned"])
    ensure_lookup(conn, "MachineMode", ["c64", "c128"])
    ensure_file_type(conn)
    ensure_lookup(conn, "LaunchMode", ["disk", "prg", "crt", "tap"])
    conn.execute("CREATE TABLE IF NOT EXISTS Tag (pk_ID INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)")
    conn.execute("CREATE TABLE IF NOT EXISTS ScanPath (path TEXT PRIMARY KEY)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS Image (
            pk_ID INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL DEFAULT '',
            entry TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            quarantine_reason TEXT NOT NULL DEFAULT '',
            deleted_reason TEXT NOT NULL DEFAULT '',
            quarantined INTEGER NOT NULL DEFAULT 0,
            fk_Status_ID INTEGER REFERENCES Status(pk_ID),
            fk_StorageStatus_ID INTEGER REFERENCES StorageStatus(pk_ID),
            fk_MachineMode_ID INTEGER REFERENCES MachineMode(pk_ID),
            fk_FileType_ID INTEGER REFERENCES FileType(pk_ID),
            fk_LaunchMode_ID INTEGER REFERENCES LaunchMode(pk_ID),
            last_seen_at TEXT,
            deleted_at TEXT,
            reappeared_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ImageTag (
            fk_Image_ID INTEGER NOT NULL REFERENCES Image(pk_ID) ON DELETE CASCADE,
            fk_Tag_ID INTEGER NOT NULL REFERENCES Tag(pk_ID) ON DELETE CASCADE,
            PRIMARY KEY (fk_Image_ID, fk_Tag_ID)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_Image_title ON Image(title)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_Image_path ON Image(path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_Image_payload ON Image(payload)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_Image_Status ON Image(fk_Status_ID)")
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_Image_updated_at
        AFTER UPDATE ON Image
        FOR EACH ROW
        WHEN NEW.updated_at = OLD.updated_at
        BEGIN
            UPDATE Image SET updated_at = CURRENT_TIMESTAMP WHERE pk_ID = NEW.pk_ID;
        END
        """
    )

    # One-time migration from the first flat prototype table, if present.
    old_game_rows = conn.execute("SELECT type FROM sqlite_master WHERE name = 'game_rows'").fetchone()
    if old_game_rows and old_game_rows[0] == "table" and conn.execute("SELECT COUNT(*) FROM Image").fetchone()[0] == 0:
        for r in conn.execute("SELECT * FROM game_rows").fetchall():
            d = dict(r)
            file_type = d.get("file_type") or d.get("type")
            conn.execute(
                """
                INSERT OR IGNORE INTO Image (
                    title, path, payload, entry, detail, notes,
                    quarantined, quarantine_reason, deleted_reason,
                    fk_Status_ID, fk_StorageStatus_ID, fk_MachineMode_ID,
                    fk_FileType_ID, fk_LaunchMode_ID
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d.get("title", ""), d.get("path", ""), d.get("payload", ""),
                    d.get("entry", ""), d.get("detail", ""), d.get("notes", ""),
                    1 if str(d.get("quarantined", "")).lower() in ("1", "true", "yes", "y") else 0,
                    d.get("quarantine_reason", ""), d.get("deleted_reason", ""),
                    lookup_id(conn, "Status", d.get("status", "")),
                    lookup_id(conn, "StorageStatus", d.get("storage_status", "present") or "present"),
                    lookup_id(conn, "MachineMode", d.get("machine_mode", "")),
                    lookup_id(conn, "FileType", file_type),
                    lookup_id(conn, "LaunchMode", d.get("mode", "")),
                ),
            )

    for legacy in ("image_rows", "game_rows"):
        obj = conn.execute("SELECT type FROM sqlite_master WHERE name = ?", (legacy,)).fetchone()
        if obj and obj[0] == "table":
            conn.execute(f"DROP TABLE {legacy}")
    conn.execute("DROP VIEW IF EXISTS image_rows")
    conn.execute("DROP VIEW IF EXISTS game_rows")
    view_sql = """
        CREATE VIEW image_rows AS
        SELECT
            i.pk_ID AS id,
            i.title,
            i.path,
            i.payload,
            lm.name AS mode,
            i.entry,
            mm.name AS machine_mode,
            ft.name AS file_type,
            ft.name AS type,
            COALESCE(ft.enabled, 1) AS file_type_enabled,
            i.detail,
            s.name AS status,
            ss.name AS storage_status,
            i.quarantined,
            i.quarantine_reason,
            i.deleted_reason,
            i.notes,
            i.created_at,
            i.updated_at
        FROM Image i
        LEFT JOIN Status s ON s.pk_ID = i.fk_Status_ID
        LEFT JOIN StorageStatus ss ON ss.pk_ID = i.fk_StorageStatus_ID
        LEFT JOIN MachineMode mm ON mm.pk_ID = i.fk_MachineMode_ID
        LEFT JOIN FileType ft ON ft.pk_ID = i.fk_FileType_ID
        LEFT JOIN LaunchMode lm ON lm.pk_ID = i.fk_LaunchMode_ID
    """
    conn.execute(view_sql)


def read_sqlite_rows(path):
    with sqlite_connect(path) as conn:
        ensure_rows_table(conn)
        rows = conn.execute("SELECT * FROM image_rows ORDER BY id").fetchall()
        return [{k: (row[k] if row[k] is not None else "") for k in row.keys() if k not in ("id", "created_at", "updated_at")} for row in rows]


def write_sqlite_rows(path, rows, fields):
    with sqlite_connect(path) as conn:
        ensure_rows_table(conn, fields)
        conn.execute("DELETE FROM Image")
        for r in rows:
            file_type = r.get("file_type") or r.get("type")
            vals = {
                "title": r.get("title", ""),
                "path": r.get("path", ""),
                "payload": r.get("payload", ""),
                "entry": r.get("entry", ""),
                "detail": r.get("detail", ""),
                "notes": r.get("notes", ""),
                "quarantine_reason": r.get("quarantine_reason", ""),
                "deleted_reason": r.get("deleted_reason", ""),
                "quarantined": 1 if str(r.get("quarantined", "")).lower() in ("1", "true", "yes", "y") else 0,
                "fk_Status_ID": lookup_id(conn, "Status", r.get("status", "")),
                "fk_StorageStatus_ID": lookup_id(conn, "StorageStatus", r.get("storage_status", "present") or "present"),
                "fk_MachineMode_ID": lookup_id(conn, "MachineMode", r.get("machine_mode", "")),
                "fk_FileType_ID": lookup_id(conn, "FileType", file_type),
                "fk_LaunchMode_ID": lookup_id(conn, "LaunchMode", r.get("mode", "")),
            }
            columns = ", ".join(vals)
            placeholders = ", ".join("?" for _ in vals)
            conn.execute(f"INSERT INTO Image ({columns}) VALUES ({placeholders})", list(vals.values()))


def import_scan_paths(db_path, paths):
    """Replace ScanPath contents with the latest full inventory path list.

    Uses sqlite3 CLI .import when available, falling back to Python executemany.
    """
    paths = [p for p in paths if p]
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        conn.execute("DELETE FROM ScanPath")

    sqlite3_bin = shutil.which("sqlite3")
    shm = Path("/dev/shm")
    tmpdir = shm if shm.is_dir() and os.access(shm, os.W_OK) else Path(tempfile.gettempdir())
    scan_file = tmpdir / f"u2_scan_paths_{os.getpid()}.txt"
    try:
        scan_file.write_text("".join(p.replace("\n", " ") + "\n" for p in paths))
        if sqlite3_bin:
            script = f"""
.bail on
.mode tabs
.import {scan_file} ScanPath
"""
            try:
                subprocess.run([sqlite3_bin, str(db_path)], input=script, text=True, check=True)
                return
            except subprocess.CalledProcessError:
                pass
        with sqlite_connect(db_path) as conn:
            conn.execute("PRAGMA synchronous=OFF")
            conn.executemany("INSERT OR IGNORE INTO ScanPath(path) VALUES (?)", ((p,) for p in paths))
    finally:
        try:
            scan_file.unlink()
        except FileNotFoundError:
            pass


def reconcile_scan_paths(db_path):
    """Update storage status from ScanPath vs Image after a full scan."""
    with sqlite_connect(db_path) as conn:
        ensure_rows_table(conn)
        present = lookup_id(conn, "StorageStatus", "present")
        missing = lookup_id(conn, "StorageStatus", "missing")
        deleted = lookup_id(conn, "StorageStatus", "deleted")
        returned = lookup_id(conn, "StorageStatus", "returned")
        conn.execute(
            "UPDATE Image SET fk_StorageStatus_ID = ? WHERE fk_StorageStatus_ID = ? AND path IN (SELECT path FROM ScanPath)",
            (present, missing),
        )
        conn.execute(
            "UPDATE Image SET fk_StorageStatus_ID = ? WHERE fk_StorageStatus_ID = ? AND path IN (SELECT path FROM ScanPath)",
            (returned, deleted),
        )
        conn.execute(
            "UPDATE Image SET fk_StorageStatus_ID = ? WHERE fk_StorageStatus_ID = ? AND path NOT IN (SELECT path FROM ScanPath)",
            (missing, present),
        )
        return {
            "scan_paths": conn.execute("SELECT COUNT(*) FROM ScanPath").fetchone()[0],
            "new_paths": conn.execute("SELECT COUNT(*) FROM ScanPath s LEFT JOIN Image i ON i.path = s.path WHERE i.path IS NULL").fetchone()[0],
            "missing_images": conn.execute("SELECT COUNT(*) FROM Image WHERE fk_StorageStatus_ID = ?", (missing,)).fetchone()[0],
            "returned_images": conn.execute("SELECT COUNT(*) FROM Image WHERE fk_StorageStatus_ID = ?", (returned,)).fetchone()[0],
        }


def file_type_names(db_path="curator.db", enabled_only=False):
    if is_sqlite_path(db_path):
        with sqlite_connect(db_path) as conn:
            ensure_rows_table(conn)
            sql = "SELECT name FROM FileType"
            if enabled_only:
                sql += " WHERE enabled = 1"
            sql += " ORDER BY name"
            return [r[0] for r in conn.execute(sql)]
    names = ["crt", "d64", "d71", "d81", "g64", "prg", "tap"]
    return [n for n in names if n not in ("g64", "tap")] if enabled_only else names


def enabled_file_type_names(db_path="curator.db"):
    return file_type_names(db_path, enabled_only=True)


def table_delimiter(path):
    # Legacy import/export: .tsv/.tab are tab-delimited, .csv is comma-delimited.
    lower = str(path).lower()
    return "\t" if lower.endswith((".tsv", ".tab")) else ","


def read_csv(path):
    if is_sqlite_path(path):
        return read_sqlite_rows(path)
    with open(path, newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        # Prefer extension, but auto-detect tab files with .csv names too.
        delim = table_delimiter(path)
        first = sample.splitlines()[0] if sample.splitlines() else ""
        if delim == "," and "\t" in first and "," not in first:
            delim = "\t"
        return list(csv.DictReader(f, delimiter=delim))


def write_csv(path, rows, fields):
    if is_sqlite_path(path):
        write_sqlite_rows(path, rows, fields)
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=table_delimiter(path))
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
