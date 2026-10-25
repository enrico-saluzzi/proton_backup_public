#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-unrecognized.py - List local photo files that match nothing on Proton.

WHY
  photo-fixdate.py counts as "unrecognized" the local files whose sha1
  matches no photo in the Proton inventory, but only prints the first 5.
  These are typically leftovers: photos deleted on Proton but still in the
  backup, old manual imports, or files converted by a browser. This script
  lists all of them, with full paths, so you can decide what to do.

WHAT IT DOES
  Applies exactly the same filter and the same check as photo-fixdate.py
  (same folders, same skipped files), hashing every local file and looking
  its sha1 up in .proton-photo-inventory.json. Progress is printed every
  100 files: hashing tens of thousands of photos and videos on an external
  disk takes a while.

  The full list is written to unrecognized-photos.txt in the backup folder.

USAGE
  ./photo-unrecognized.py /path/to/backup/proton-photos

SAFETY
  Read-only: changes nothing, only writes the list file.
"""

import argparse
import json
import os
import sys
import time

import protonlib

INVENTORY = ".proton-photo-inventory.json"
OUTPUT = "unrecognized-photos.txt"


def parse_args():
    p = argparse.ArgumentParser(
        description="List local photo files whose content matches nothing on Proton.")
    p.add_argument("dest", help="photo backup folder")
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    return a


def main():
    a = parse_args()
    inv_path = os.path.join(a.dest, INVENTORY)
    if not os.path.exists(inv_path):
        sys.exit(f"ERROR: {inv_path} not found. Run photo-backup.py first.")
    with open(inv_path, encoding="utf-8") as f:
        inv = json.load(f)

    hashes = set()
    for x in inv:
        h = ((x.get("activeRevision") or {}).get("claimedDigests") or {}).get("sha1")
        if h:
            hashes.add(h)

    album_dir = os.path.join(a.dest, "album")
    files = []
    for dp, dn, fn in os.walk(a.dest):
        if dp.startswith(album_dir):
            continue
        for f in fn:
            if f.startswith(".") or f.endswith(".txt") or f.endswith(".json"):
                continue
            files.append(os.path.join(dp, f))

    print(f"  {len(files)} local files to check...")
    unrecognized = []
    t0 = time.time()
    for i, p in enumerate(files, 1):
        h = protonlib.sha1_of(p)
        if h not in hashes:
            unrecognized.append(p)
        if i % 100 == 0 or i == len(files):
            el = int(time.time() - t0)
            sys.stderr.write(f"\r  {i}/{len(files)} files | {el}s      ")
            sys.stderr.flush()
    sys.stderr.write("\n")

    print(f"{len(unrecognized)} unrecognized files")
    out = os.path.join(a.dest, OUTPUT)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(unrecognized) + ("\n" if unrecognized else ""))
    print(f"Full list: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
