#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-unlinked.py - Which Proton photos have no matching local file, and why.

WHY
  photo-backup.py decides what is "already downloaded" from its state file,
  which records uids but doesn't check that the file is still physically
  on disk. If a backup file disappears for any reason other than
  photo-prune.py, the backup keeps saying "in sync" forever. album-backup.py
  only notices it for photos that belong to an album ("not linked"). This
  script checks every photo in the inventory.

WHAT IT DOES
  Cross-references .proton-photo-inventory.json (what Proton says it holds)
  with .proton-photo-map.json (what photo-fixdate.py matched by content)
  and separates two very different cases:

  - photos WITHOUT a sha1 in the inventory: they can't be matched by content
    by definition, whatever is on disk. They aren't necessarily missing; this
    method just can't confirm them.

  - photos WITH a sha1 but no matching local file: this is the case that
    matters. Their content is nowhere in the backup.

  Each photo is shown with name, expected year and albums. The full list is
  written to unlinked-photos.txt in the backup folder. Run photo-fixdate.py
  first, so the map is up to date.

USAGE
  ./photo-unlinked.py /path/to/backup/proton-photos

SAFETY
  Read-only: doesn't download, change or delete anything; only writes the
  list file. Instant: it only reads JSON files already saved.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

INVENTORY = ".proton-photo-inventory.json"
PHOTO_MAP = ".proton-photo-map.json"
ALBUM_MAP = ".proton-albums.json"
OUTPUT = "unlinked-photos.txt"


def parse_args():
    p = argparse.ArgumentParser(
        description="Which Proton photos have no matching local file, and why.")
    p.add_argument("dest", help="photo backup folder")
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    return a


def capture_time(x):
    ph = x.get("photo") or {}
    if ph.get("captureTime"):
        return ph["captureTime"]
    rev = x.get("activeRevision") or {}
    m = rev.get("claimedAdditionalMetadata") or {}
    cam = m.get("Camera") or {}
    if cam.get("CaptureTime"):
        return cam["CaptureTime"]
    if x.get("captureTime"):
        return x["captureTime"]
    return rev.get("claimedModificationTime") or x.get("creationTime")


def year_of(ts):
    if not ts:
        return "no-date"
    try:
        return str(datetime.fromisoformat(ts.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).year)
    except Exception:
        return "no-date"


def main():
    a = parse_args()
    inv_path = os.path.join(a.dest, INVENTORY)
    if not os.path.exists(inv_path):
        sys.exit(f"ERROR: {inv_path} not found. Run photo-backup.py first.")
    with open(inv_path, encoding="utf-8") as f:
        inv = json.load(f)

    map_path = os.path.join(a.dest, PHOTO_MAP)
    photo_map = {}
    if os.path.exists(map_path):
        with open(map_path, encoding="utf-8") as f:
            photo_map = json.load(f)
    else:
        print(f"  WARNING: {PHOTO_MAP} not found: run photo-fixdate.py first.")

    albums_of = {}
    albums_path = os.path.join(a.dest, ALBUM_MAP)
    if os.path.exists(albums_path):
        with open(albums_path, encoding="utf-8") as f:
            albums = json.load(f)
        for al in albums:
            for u in al.get("photos", []):
                albums_of.setdefault(u, []).append(al["name"])

    no_hash = []
    no_file = []

    for x in inv:
        uid = x.get("uid") or x.get("nodeUid")
        if not uid:
            continue
        rev = x.get("activeRevision") or {}
        h = (rev.get("claimedDigests") or {}).get("sha1")
        name = (x.get("name") or {}).get("value") or "(unknown name)"
        year = year_of(capture_time(x))
        in_albums = ", ".join(albums_of.get(uid, [])) or "-"

        if not h:
            no_hash.append((uid, name, year, in_albums))
            continue

        path = photo_map.get(uid)
        if not path or not os.path.exists(path):
            no_file.append((uid, name, year, in_albums))

    print(f"Inventory                              : {len(inv)} photos")
    print(f"Without sha1 (can't match by content)  : {len(no_hash)}")
    print(f"With sha1 but no matching local file   : {len(no_file)}")
    print("")

    def dump(title, items):
        if not items:
            return
        print(title)
        for uid, name, year, in_albums in items:
            print(f"  {uid[-12:]}  {year}  {name:<40}  albums: {in_albums}")
        print("")

    dump("WITHOUT SHA1 (Proton provides no hash to compare):", no_hash)
    dump("WITH SHA1 BUT NO MATCHING LOCAL FILE:", no_file)

    out = os.path.join(a.dest, OUTPUT)
    with open(out, "w", encoding="utf-8") as f:
        f.write("# without sha1 (can't match by content)\n")
        for uid, name, year, in_albums in no_hash:
            f.write(f"{uid}\t{name}\t{year}\talbums: {in_albums}\n")
        f.write("\n# with sha1 but no matching local file\n")
        for uid, name, year, in_albums in no_file:
            f.write(f"{uid}\t{name}\t{year}\talbums: {in_albums}\n")
    print(f"Full list: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
