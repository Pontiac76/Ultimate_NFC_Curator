#!/usr/bin/env python3
"""Export approved game titles for label printing."""

import argparse
import csv
from u2_common import read_csv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="?", default="curator.db")
    ap.add_argument("--out", default="labels.csv")
    args = ap.parse_args()
    rows = [r for r in read_csv(args.manifest) if r.get("status") == "approved"]
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["title"])
        w.writeheader()
        for r in rows:
            w.writerow({"title": r.get("title", "")})
    print(f"Wrote {len(rows)} approved labels to {args.out}")


if __name__ == "__main__":
    main()
