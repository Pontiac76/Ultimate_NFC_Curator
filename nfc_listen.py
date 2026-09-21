#!/usr/bin/env python3
"""
Listen for NFC-A/ISO14443A taps using a PN532 on a serial UART.

Default device: /dev/ttyUSB0 at 115200 baud.
Prints the raw PN532 response and a small decoded summary for each tap.
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
    """Build a normal PN532 frame. payload should start with TFI (0xD4)."""
    length = len(payload)
    return b"\x00\x00\xff" + bytes([length, (-length) & 0xFF]) + payload + bytes([checksum(payload), 0x00])


def open_serial(path, baud):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)

    # raw 8N1
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


def read_exactish(fd, timeout=1.0):
    """Read whatever arrives until timeout has been quiet for a short period."""
    out = bytearray()
    deadline = time.time() + timeout
    quiet_deadline = None

    while time.time() < deadline:
        wait = 0.05
        r, _, _ = select.select([fd], [], [], wait)
        if r:
            chunk = os.read(fd, 4096)
            if chunk:
                out.extend(chunk)
                quiet_deadline = time.time() + 0.03
        elif quiet_deadline and time.time() >= quiet_deadline:
            break

    return bytes(out)


def extract_frames(blob):
    """Return (acks, payloads, leftovers) from a byte stream."""
    acks = 0
    payloads = []
    i = 0

    while i < len(blob):
        if blob.startswith(ACK, i):
            acks += 1
            i += len(ACK)
            continue

        # find preamble/start code
        start = blob.find(b"\x00\x00\xff", i)
        if start < 0:
            break
        i = start + 3
        if i + 2 > len(blob):
            break

        length = blob[i]
        lcs = blob[i + 1]
        i += 2

        # Skip ACK-like frame already handled, invalid len checksum, or extended frames.
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

    return acks, payloads, blob[i:]


def transceive(fd, cmd, timeout=1.0):
    """Send PN532 command bytes without TFI. Return raw bytes read and response payloads."""
    # The extra 0x55/0x00 bytes wake/sync PN532 HSU (UART) modules. They are
    # harmless when already awake and make startup much more reliable.
    os.write(fd, b"\x55\x55\x00\x00\x00" + frame(bytes([HOST_TFI]) + bytes(cmd)))
    raw = read_exactish(fd, timeout)
    _, payloads, _ = extract_frames(raw)
    return raw, payloads


def require_response(fd, cmd, expected_code, timeout=1.0):
    raw, payloads = transceive(fd, cmd, timeout)
    for p in payloads:
        if len(p) >= 2 and p[0] == PN532_TFI and p[1] == expected_code:
            return raw, p
    raise RuntimeError(f"No expected PN532 response 0x{expected_code:02x}; raw={raw.hex(' ')}")


def decode_type_a_response(payload):
    """Decode D5 4B InListPassiveTarget response enough to show UID/card info."""
    # D5 4B NbTg [Tg SENS_RES(2) SEL_RES NFCIDLen NFCID ...]
    if len(payload) < 3 or payload[0] != PN532_TFI or payload[1] != 0x4B:
        return None
    count = payload[2]
    if count < 1 or len(payload) < 10:
        return {"count": count}

    idx = 3
    target = payload[idx]
    sens_res = payload[idx + 1 : idx + 3]
    sel_res = payload[idx + 3]
    uid_len = payload[idx + 4]
    uid_start = idx + 5
    uid = payload[uid_start : uid_start + uid_len]

    return {
        "count": count,
        "target": target,
        "sens_res": sens_res.hex(" "),
        "sel_res": f"0x{sel_res:02x}",
        "uid": uid.hex(":"),
    }


URI_PREFIXES = [
    "", "http://www.", "https://www.", "http://", "https://", "tel:", "mailto:",
    "ftp://anonymous:anonymous@", "ftp://ftp.", "ftps://", "sftp://", "smb://",
    "nfs://", "ftp://", "dav://", "news:", "telnet://", "imap:", "rtsp://",
    "urn:", "pop:", "sip:", "sips:", "tftp:", "btspp://", "btl2cap://",
    "btgoep://", "tcpobex://", "irdaobex://", "file://", "urn:epc:id:",
    "urn:epc:tag:", "urn:epc:pat:", "urn:epc:raw:", "urn:epc:", "urn:nfc:",
]


def read_ntag_memory(fd, max_page=80):
    """Read NTAG/Mifare Ultralight-style memory. Returns concatenated bytes."""
    data = bytearray()
    # READ 0 reads pages 0-3; READ 4 reads 4-7, etc.
    for page in range(0, max_page + 1, 4):
        raw, payloads = transceive(fd, [0x40, 0x01, 0x30, page], timeout=0.5)
        got = None
        for p in payloads:
            # D5 41 status data(16)
            if len(p) >= 3 and p[0] == PN532_TFI and p[1] == 0x41 and p[2] == 0x00:
                got = p[3:]
                break
        if not got:
            break
        data.extend(got)
        # NDEF terminator TLV. Usually in user area after page 4.
        if page >= 4 and 0xFE in got:
            break
    return bytes(data)


def parse_ndef_tlv(memory):
    """Find NDEF TLV in NTAG memory and return the NDEF message bytes."""
    # NTAG user area starts at page 4 = byte offset 16.
    i = 16
    while i < len(memory):
        t = memory[i]
        i += 1
        if t == 0x00:  # NULL TLV
            continue
        if t == 0xFE:  # Terminator
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
        if t == 0x03:  # NDEF Message TLV
            return bytes(value)
        i += length
    return None


def decode_ndef_message(msg):
    """Decode common NDEF Text and URI records."""
    out = []
    i = 0
    while i < len(msg):
        header = msg[i]
        i += 1
        sr = bool(header & 0x10)
        il = bool(header & 0x08)
        tnf = header & 0x07
        if i >= len(msg):
            break
        type_len = msg[i]
        i += 1
        if sr:
            if i >= len(msg):
                break
            payload_len = msg[i]
            i += 1
        else:
            if i + 4 > len(msg):
                break
            payload_len = int.from_bytes(msg[i : i + 4], "big")
            i += 4
        id_len = msg[i] if il and i < len(msg) else 0
        if il:
            i += 1
        typ = msg[i : i + type_len]
        i += type_len
        rec_id = msg[i : i + id_len]
        i += id_len
        payload = msg[i : i + payload_len]
        i += payload_len

        decoded = None
        if tnf == 0x01 and typ == b"U" and payload:
            prefix = URI_PREFIXES[payload[0]] if payload[0] < len(URI_PREFIXES) else ""
            decoded = prefix + payload[1:].decode("utf-8", "replace")
        elif tnf == 0x01 and typ == b"T" and payload:
            status = payload[0]
            lang_len = status & 0x3F
            decoded = payload[1 + lang_len :].decode("utf-8", "replace")
        else:
            decoded = payload.decode("utf-8", "replace") if payload else ""

        out.append({
            "type": typ.decode("ascii", "replace"),
            "tnf": tnf,
            "payload_hex": payload.hex(" "),
            "decoded": decoded,
        })
        if header & 0x40:  # ME, message end
            break
    return out


def main():
    parser = argparse.ArgumentParser(description="Listen for PN532 NFC taps and print what was received.")
    parser.add_argument("--device", "-d", default="/dev/ttyUSB0", help="serial device, default /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200, choices=[9600, 19200, 38400, 57600, 115200])
    parser.add_argument("--poll-delay", type=float, default=0.25, help="seconds between polls")
    args = parser.parse_args()

    baud_const = getattr(termios, f"B{args.baud}")
    fd = open_serial(args.device, baud_const)

    try:
        print(f"Listening on {args.device} at {args.baud} baud. Tap an NFC tag/card...", flush=True)

        # Prove the chip is there.
        termios.tcflush(fd, termios.TCIOFLUSH)
        raw, fw = require_response(fd, [0x02], 0x03, timeout=1.0)  # GetFirmwareVersion
        print(f"PN532 firmware response: raw={raw.hex(' ')} payload={fw.hex(' ')}", flush=True)

        # SAMConfiguration: normal mode, timeout ~= 1s, IRQ enabled.
        raw, sam = require_response(fd, [0x14, 0x01, 0x14, 0x01], 0x15, timeout=1.0)
        print(f"SAM configured: raw={raw.hex(' ')} payload={sam.hex(' ')}", flush=True)

        last_uid = None
        last_seen = 0.0

        while True:
            # InListPassiveTarget: max 1 target, 106 kbps type A (Mifare/ISO14443A)
            raw, payloads = transceive(fd, [0x4A, 0x01, 0x00], timeout=1.2)
            hit = None
            for p in payloads:
                decoded = decode_type_a_response(p)
                if decoded and decoded.get("count", 0) >= 1:
                    hit = (p, decoded)
                    break

            if hit:
                p, decoded = hit
                uid = decoded.get("uid")
                now = time.time()
                # Avoid flooding the same card every poll; print it again after it has been away/a while.
                if uid != last_uid or now - last_seen > 2.0:
                    print("\nTap received:", flush=True)
                    print(f"  raw_read: {raw.hex(' ')}", flush=True)
                    print(f"  payload:  {p.hex(' ')}", flush=True)
                    for k, v in decoded.items():
                        print(f"  {k}: {v}", flush=True)

                    memory = read_ntag_memory(fd)
                    print(f"  tag_memory: {memory.hex(' ')}", flush=True)
                    ndef = parse_ndef_tlv(memory)
                    if ndef:
                        print(f"  ndef: {ndef.hex(' ')}", flush=True)
                        for record in decode_ndef_message(ndef):
                            print(f"  ndef_record_type: {record['type']}", flush=True)
                            print(f"  ndef_payload: {record['payload_hex']}", flush=True)
                            print(f"  decoded: {record['decoded']}", flush=True)
                    else:
                        print("  ndef: <none found>", flush=True)
                last_uid = uid
                last_seen = now
            elif time.time() - last_seen > 1.0:
                last_uid = None

            time.sleep(args.poll_delay)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
