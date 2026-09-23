#!/usr/bin/env python3
"""List/delete files in Ultimate /Temp over FTP."""

import argparse
import ftplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from u2_common import resolve_ultimate


def clean_temp(host, dry_run=False):
    ftp = ftplib.FTP(host, timeout=10)
    ftp.login("anonymous", "ftp@example.com")
    try:
        ftp.cwd("/Temp")
        names = ftp.nlst()
        # Some FTP servers include . and ..; ignore directories if delete fails.
        names = [n for n in names if n not in (".", "..")]
        if not names:
            print("/Temp is empty")
            return 0
        deleted = 0
        for name in names:
            if dry_run:
                print(f"would delete /Temp/{name}")
                continue
            try:
                ftp.delete(name)
                print(f"deleted /Temp/{name}")
                deleted += 1
            except Exception as e:
                print(f"failed /Temp/{name}: {e}")
        return deleted
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ultimate", default="auto")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    args.ultimate = resolve_ultimate(args.ultimate)
    n = clean_temp(args.ultimate, args.dry_run)
    print(f"cleaned {n} file(s)")


if __name__ == "__main__":
    main()
