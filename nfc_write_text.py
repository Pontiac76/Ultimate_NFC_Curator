#!/usr/bin/env python3
"""
One-shot NDEF text writer for PN532 UART + NTAG/Mifare Ultralight tags.

Usage:
  sudo ./nfc_write_text.py 'U2+:prg:/Usb0/C64/Games/Ghostbusters/ghostbusters.d64#ghostbusters'

This overwrites the NDEF user area starting at page 4. It does NOT write lock bytes.
"""

import argparse
import os
import select
import sys
import termios
import time

ACK = b"\x00\x00\xff\x00\xff\x00"
HOST_TFI = 0xD4
PN532_TFI = 0xD5


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
            return raw, p
    raise RuntimeError(f"No expected PN532 response 0x{expected_code:02x}; raw={raw.hex(' ')}")


def find_tag(fd):
    raw, payloads = transceive(fd, [0x4A, 0x01, 0x00], timeout=1.2)  # InListPassiveTarget, type A
    for p in payloads:
        if len(p) >= 10 and p[0] == PN532_TFI and p[1] == 0x4B and p[2] >= 1:
            uid_len = p[7]
            uid = p[8 : 8 + uid_len]
            return uid, raw, p
    return None, raw, None


def tag_read(fd, page):
    raw, payloads = transceive(fd, [0x40, 0x01, 0x30, page], timeout=0.5)  # InDataExchange READ
    for p in payloads:
        if len(p) >= 19 and p[0] == PN532_TFI and p[1] == 0x41 and p[2] == 0x00:
            return p[3:19]
    raise RuntimeError(f"READ page {page} failed; raw={raw.hex(' ')}")


def tag_write_page(fd, page, four):
    if len(four) != 4:
        raise ValueError("page write requires exactly four bytes")
    raw, payloads = transceive(fd, [0x40, 0x01, 0xA2, page] + list(four), timeout=0.5)
    for p in payloads:
        if len(p) >= 3 and p[0] == PN532_TFI and p[1] == 0x41 and p[2] == 0x00:
            return
    raise RuntimeError(f"WRITE page {page} failed; raw={raw.hex(' ')}")


def ndef_text_record(text, lang="en"):
    text_b = text.encode("utf-8")
    lang_b = lang.encode("ascii")
    if len(lang_b) > 63:
        raise ValueError("language code too long")
    payload = bytes([len(lang_b)]) + lang_b + text_b
    if len(payload) > 255:
        raise ValueError("text too long for short NDEF record")
    # MB|ME|SR + TNF Well Known, type='T'
    return bytes([0xD1, 0x01, len(payload), 0x54]) + payload


def ndef_tlv(message):
    if len(message) < 255:
        return bytes([0x03, len(message)]) + message + b"\xFE"
    return bytes([0x03, 0xFF, (len(message) >> 8) & 0xFF, len(message) & 0xFF]) + message + b"\xFE"


def main():
    parser = argparse.ArgumentParser(description="Write one NDEF text string to an NTAG/Mifare Ultralight tag via PN532.")
    parser.add_argument("text", help="text to write to the NFC tag")
    parser.add_argument("--device", "-d", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200, choices=[9600, 19200, 38400, 57600, 115200])
    parser.add_argument("--max-user-bytes", type=int, default=144, help="default 144 bytes = NTAG213 user area")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()

    msg = ndef_text_record(args.text)
    tlv = ndef_tlv(msg)
    padded = tlv + bytes((-len(tlv)) % 4)
    pages = len(padded) // 4

    if len(padded) > args.max_user_bytes:
        raise SystemExit(f"Payload needs {len(padded)} user bytes, max is {args.max_user_bytes}. Refusing.")

    print(f"Text: {args.text}")
    print(f"NDEF bytes: {len(msg)}; TLV+padded bytes: {len(padded)}; pages 4-{4 + pages - 1}")
    print("This will overwrite the NDEF area of the presented tag.")
    if not args.yes:
        ans = input("Proceed? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            raise SystemExit("Cancelled.")

    fd = open_serial(args.device, getattr(termios, f"B{args.baud}"))
    try:
        termios.tcflush(fd, termios.TCIOFLUSH)
        raw, fw = require_response(fd, [0x02], 0x03, timeout=1.0)
        print(f"PN532 firmware: {fw.hex(' ')}")
        require_response(fd, [0x14, 0x01, 0x14, 0x01], 0x15, timeout=1.0)

        print("Waiting for tag...")
        uid = None
        while uid is None:
            uid, _, _ = find_tag(fd)
            if uid is None:
                time.sleep(0.2)
        print(f"Tag found UID: {uid.hex(':')}")

        # Read page 3 for informational CC bytes only; do not modify it.
        try:
            pages0_3 = tag_read(fd, 0)
            print(f"Pages 0-3: {pages0_3.hex(' ')}")
        except Exception as e:
            print(f"Warning: could not read initial pages: {e}")

        for i in range(pages):
            page = 4 + i
            data = padded[i * 4 : i * 4 + 4]
            tag_write_page(fd, page, data)
            print(f"Wrote page {page:02d}: {data.hex(' ')}")

        # Verify exactly the written area.
        verify = bytearray()
        for page in range(4, 4 + pages, 4):
            verify.extend(tag_read(fd, page))
        verify = bytes(verify[: len(padded)])
        if verify != padded:
            print("Verify FAILED")
            print(f"expected: {padded.hex(' ')}")
            print(f"readback: {verify.hex(' ')}")
            raise SystemExit(1)
        print("Write verified OK.")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
