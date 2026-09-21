#!/usr/bin/env python3
"""
Listen for NFC tags containing U2+ launch strings, then call the Ultimate-II+ API.

NFC text format:
  U2+:prg:/Usb0/C64/Games/Ghostbusters/ghostbusters.d64#ghostbusters
  U2+:crt:/Usb0/C64/crt/Planet_X2.1_GMod2.crt

Notes:
- prg + .d64#entry extracts that PRG from the D64 and POSTs it to /v1/runners:run_prg.
- crt downloads the CRT and POSTs it to /v1/runners:run_crt.
- d64 mode is detected but not executed yet; mounting a disk image is a different API path than
  the PRG/CRT runner endpoint and needs to be confirmed for this firmware.
"""

import argparse
import os
import select
import sys
import tempfile
import termios
import time
import urllib.parse
import urllib.request

try:
    from u2_common import discover_ultimate, ftp_download as common_ftp_download
except Exception:
    discover_ultimate = None
    common_ftp_download = None

ACK = b"\x00\x00\xff\x00\xff\x00"
HOST_TFI = 0xD4
PN532_TFI = 0xD5
URI_PREFIXES = [
    "", "http://www.", "https://www.", "http://", "https://", "tel:", "mailto:",
    "ftp://anonymous:anonymous@", "ftp://ftp.", "ftps://", "sftp://", "smb://",
    "nfs://", "ftp://", "dav://", "news:", "telnet://", "imap:", "rtsp://",
    "urn:", "pop:", "sip:", "sips:", "tftp:", "btspp://", "btl2cap://",
    "btgoep://", "tcpobex://", "irdaobex://", "file://", "urn:epc:id:",
    "urn:epc:tag:", "urn:epc:pat:", "urn:epc:raw:", "urn:epc:", "urn:nfc:",
]


def checksum(bytes_):
    return (-sum(bytes_)) & 0xFF


def frame(payload):
    length = len(payload)
    return b"\x00\x00\xff" + bytes([length, (-length) & 0xFF]) + payload + bytes([checksum(payload), 0x00])


def open_serial(path, baud):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = baud
    attrs[5] = baud
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_quiet(fd, timeout=1.0):
    out = bytearray()
    deadline = time.time() + timeout
    quiet_deadline = None
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            chunk = os.read(fd, 4096)
            if chunk:
                out.extend(chunk)
                quiet_deadline = time.time() + 0.03
        elif quiet_deadline and time.time() >= quiet_deadline:
            break
    return bytes(out)


def extract_frames(blob):
    payloads = []
    i = 0
    while i < len(blob):
        if blob.startswith(ACK, i):
            i += len(ACK)
            continue
        start = blob.find(b"\x00\x00\xff", i)
        if start < 0:
            break
        i = start + 3
        if i + 2 > len(blob):
            break
        length = blob[i]
        lcs = blob[i + 1]
        i += 2
        if ((length + lcs) & 0xFF) != 0 or length == 0xFF:
            continue
        if i + length + 2 > len(blob):
            break
        payload = blob[i : i + length]
        dcs = blob[i + length]
        postamble = blob[i + length + 1]
        i += length + 2
        if ((sum(payload) + dcs) & 0xFF) == 0 and postamble == 0:
            payloads.append(bytes(payload))
    return payloads


def transceive(fd, cmd, timeout=1.0):
    os.write(fd, b"\x55\x55\x00\x00\x00" + frame(bytes([HOST_TFI]) + bytes(cmd)))
    raw = read_quiet(fd, timeout)
    return raw, extract_frames(raw)


def require_response(fd, cmd, expected_code, timeout=1.0):
    raw, payloads = transceive(fd, cmd, timeout)
    for p in payloads:
        if len(p) >= 2 and p[0] == PN532_TFI and p[1] == expected_code:
            return p
    raise RuntimeError(f"No expected PN532 response 0x{expected_code:02x}; raw={raw.hex(' ')}")


def find_tag(fd):
    _, payloads = transceive(fd, [0x4A, 0x01, 0x00], timeout=1.0)
    for p in payloads:
        if len(p) >= 10 and p[0] == PN532_TFI and p[1] == 0x4B and p[2] >= 1:
            uid_len = p[7]
            return p[8 : 8 + uid_len]
    return None


def tag_read(fd, page):
    raw, payloads = transceive(fd, [0x40, 0x01, 0x30, page], timeout=0.5)
    for p in payloads:
        if len(p) >= 19 and p[0] == PN532_TFI and p[1] == 0x41 and p[2] == 0x00:
            return p[3:19]
    raise RuntimeError(f"READ page {page} failed; raw={raw.hex(' ')}")


def read_ntag_memory(fd, max_page=80):
    data = bytearray()
    for page in range(0, max_page + 1, 4):
        chunk = tag_read(fd, page)
        data.extend(chunk)
        if page >= 4 and 0xFE in chunk:
            break
    return bytes(data)


def parse_ndef_tlv(memory):
    i = 16  # page 4
    while i < len(memory):
        t = memory[i]
        i += 1
        if t == 0x00:
            continue
        if t == 0xFE:
            return None
        if i >= len(memory):
            return None
        length = memory[i]
        i += 1
        if length == 0xFF:
            if i + 2 > len(memory):
                return None
            length = (memory[i] << 8) | memory[i + 1]
            i += 2
        value = memory[i : i + length]
        if t == 0x03:
            return bytes(value)
        i += length
    return None


def decode_first_ndef_text_or_uri(msg):
    if not msg:
        return None
    i = 0
    while i < len(msg):
        header = msg[i]
        i += 1
        sr = bool(header & 0x10)
        il = bool(header & 0x08)
        tnf = header & 0x07
        if i >= len(msg):
            return None
        type_len = msg[i]
        i += 1
        if sr:
            payload_len = msg[i]
            i += 1
        else:
            payload_len = int.from_bytes(msg[i : i + 4], "big")
            i += 4
        id_len = msg[i] if il else 0
        if il:
            i += 1
        typ = msg[i : i + type_len]
        i += type_len + id_len
        payload = msg[i : i + payload_len]
        i += payload_len

        if tnf == 0x01 and typ == b"T" and payload:
            lang_len = payload[0] & 0x3F
            return payload[1 + lang_len :].decode("utf-8", "replace")
        if tnf == 0x01 and typ == b"U" and payload:
            prefix = URI_PREFIXES[payload[0]] if payload[0] < len(URI_PREFIXES) else ""
            return prefix + payload[1:].decode("utf-8", "replace")
        if header & 0x40:
            break
    return None


def ftp_url(host, ultimate_path):
    # Quote each path component but preserve slashes.
    return f"ftp://{host}" + urllib.parse.quote(ultimate_path, safe="/")


def download_from_ultimate(host, ultimate_path):
    if common_ftp_download:
        return common_ftp_download(host, ultimate_path)
    url = ftp_url(host, ultimate_path)
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read()


def petscii_name(bs):
    bs = bytes(bs).split(b"\xa0")[0]
    out = ""
    for b in bs:
        if 65 <= b <= 90:
            out += chr(b).lower()
        elif 193 <= b <= 218:
            out += chr(b - 128).lower()
        elif 32 <= b <= 126:
            out += chr(b)
    return out.strip()


def extract_prg_from_d64(img, wanted_name):
    wanted = wanted_name.strip().lower()
    spt = [0] + [21] * 17 + [19] * 7 + [18] * 6 + [17] * 5
    offsets = [0]
    pos = 0
    for track in range(1, 36):
        offsets.append(pos)
        pos += spt[track] * 256

    def tso(track, sector):
        return offsets[track] + sector * 256

    start = None
    t, sec = 18, 1
    seen = set()
    available = []
    while t and (t, sec) not in seen:
        seen.add((t, sec))
        block = img[tso(t, sec) : tso(t, sec) + 256]
        for i in range(8):
            e = block[2 + i * 32 : 2 + (i + 1) * 32]
            if e[0] and (e[0] & 7) == 2:
                nm = petscii_name(e[3:19])
                available.append(nm)
                if nm.lower() == wanted:
                    start = (e[1], e[2], nm)
                    break
        if start:
            break
        t, sec = block[0], block[1]

    if not start:
        raise RuntimeError(f"PRG {wanted_name!r} not found in D64. Available PRGs: {available}")

    data = bytearray()
    t, sec, actual_name = start
    seen = set()
    while t and (t, sec) not in seen:
        seen.add((t, sec))
        block = img[tso(t, sec) : tso(t, sec) + 256]
        nt, ns = block[0], block[1]
        if nt == 0:
            data.extend(block[2 : ns + 1])
            break
        data.extend(block[2:])
        t, sec = nt, ns
    return actual_name, bytes(data)


def post_to_runner(host, endpoint, body):
    url = f"http://{host}{endpoint}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode("utf-8", "replace")


def parse_launch_text(text):
    if not text.startswith("U2+:"):
        return None
    rest = text[4:]
    parts = rest.split(":", 1)
    if len(parts) != 2:
        raise ValueError("Expected U2+:<mode>:<path>[#entry]")
    mode = parts[0]
    path_entry = parts[1]
    path, sep, entry = path_entry.partition("#")
    return mode, path, entry or None


def launch(host, text, allow_d64_prg_loader=False):
    parsed = parse_launch_text(text)
    if not parsed:
        print(f"Ignoring non-U2+ tag: {text!r}")
        return
    mode, path, entry = parsed
    print(f"Launch request: mode={mode!r} path={path!r} entry={entry!r}")

    if mode == "prg":
        blob = download_from_ultimate(host, path)
        if path.lower().endswith(".d64"):
            if not entry:
                raise RuntimeError("prg mode with a D64 requires #entry")
            actual, prg = extract_prg_from_d64(blob, entry)
            load = prg[0] + 256 * prg[1] if len(prg) >= 2 else None
            print(f"Extracted PRG {actual!r}: {len(prg)} bytes, load=${load:04x}")
            status, body = post_to_runner(host, "/v1/runners:run_prg", prg)
        else:
            print(f"Downloaded PRG: {len(blob)} bytes")
            status, body = post_to_runner(host, "/v1/runners:run_prg", blob)
        print(f"Ultimate response: HTTP {status} {body.strip()}")
        return

    if mode == "crt":
        blob = download_from_ultimate(host, path)
        print(f"Downloaded CRT: {len(blob)} bytes")
        status, body = post_to_runner(host, "/v1/runners:run_crt", blob)
        print(f"Ultimate response: HTTP {status} {body.strip()}")
        return

    if mode == "d64":
        if not allow_d64_prg_loader:
            raise RuntimeError(
                "d64 mode needs a confirmed Ultimate disk-mount API. "
                "For a one-file/loader demo you can rerun with --d64-as-prg-loader, "
                "but multi-file games may fail after the loader starts."
            )
        if not path.lower().endswith(".d64"):
            raise RuntimeError(f"{path} is not a D64; this needs real disk-image mounting support")
        if not entry:
            raise RuntimeError("--d64-as-prg-loader requires #entry")
        blob = download_from_ultimate(host, path)
        actual, prg = extract_prg_from_d64(blob, entry)
        if not prg:
            raise RuntimeError(f"Extracted PRG is 0 bytes: {entry}")
        load = prg[0] + 256 * prg[1] if len(prg) >= 2 else None
        print(f"D64 fallback: extracted loader {actual!r}: {len(prg)} bytes, load=${load:04x}")
        status, body = post_to_runner(host, "/v1/runners:run_prg", prg)
        print(f"Ultimate response: HTTP {status} {body.strip()}")
        return

    raise RuntimeError(f"Unknown U2+ mode: {mode!r}")


def main():
    ap = argparse.ArgumentParser(description="NFC-to-Ultimate-II+ launcher")
    ap.add_argument("--device", "-d", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200, choices=[9600, 19200, 38400, 57600, 115200])
    ap.add_argument("--ultimate", default="auto", help="Ultimate-II+ hostname/IP, or auto")
    ap.add_argument("--d64-as-prg-loader", action="store_true", help="fallback: extract #entry from d64 and run_prg")
    ap.add_argument("--once", action="store_true", help="exit after first valid U2+ tag")
    args = ap.parse_args()

    if args.ultimate == "auto":
        if not discover_ultimate:
            raise SystemExit("auto discovery unavailable; pass --ultimate <ip>")
        args.ultimate = discover_ultimate()
        if not args.ultimate:
            raise SystemExit("Could not discover Ultimate-II+")

    fd = open_serial(args.device, getattr(termios, f"B{args.baud}"))
    last_text = None
    last_seen = 0.0
    try:
        termios.tcflush(fd, termios.TCIOFLUSH)
        fw = require_response(fd, [0x02], 0x03, timeout=1.0)
        require_response(fd, [0x14, 0x01, 0x14, 0x01], 0x15, timeout=1.0)
        print(f"PN532 ready: {fw.hex(' ')}")
        print(f"Listening for U2+ NFC tags. Ultimate: {args.ultimate}")

        while True:
            uid = find_tag(fd)
            if uid:
                try:
                    memory = read_ntag_memory(fd)
                    text = decode_first_ndef_text_or_uri(parse_ndef_tlv(memory))
                    now = time.time()
                    if text and (text != last_text or now - last_seen > 3.0):
                        print(f"\nTag UID {uid.hex(':')} text: {text}")
                        launch(args.ultimate, text, allow_d64_prg_loader=args.d64_as_prg_loader)
                        last_text = text
                        last_seen = now
                        if args.once and text.startswith("U2+:"):
                            return
                except Exception as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                    last_seen = time.time()
            elif time.time() - last_seen > 1.0:
                last_text = None
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
