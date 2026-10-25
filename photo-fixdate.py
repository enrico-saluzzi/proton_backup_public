#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-fixdate.py - Restore capture dates on downloaded photos and sort them by year.

WHY
  Downloading keeps a photo's content but not its date: on disk every photo
  carries the date of the backup, so the library looks shuffled. The
  inventory saved by photo-backup.py, however, holds `photo.captureTime`, the
  real capture date.

WHAT IT DOES
  1. sets the capture date as the file's modification time
  2. with --reorganize, moves each photo into the folder of its CAPTURE year
     (useful if an earlier download sorted photos by a different date, e.g.
     claimedModificationTime, which for many photos is a later import date)
  3. saves .proton-photo-map.json: the uid -> local file map, built on
     content (sha1) rather than names

  The map is the key piece for the other scripts: album-backup.py uses it to
  rebuild albums, photo-prune.py to know which uid a deleted file belonged
  to, photo-redate.py to find local files to fix. Regenerate it after any
  change to the local library (pruning, moving files, new downloads).

WHY SHA1 AND NOT NAMES
  With duplicate names renamed by the CLI ("IMG_0001 (1).jpg"), a name is not
  a reliable key. Several distinct photos on Proton can also share identical
  content (the same picture uploaded from two phones): the index keeps all of
  them, giving each uid its own copy when one exists, otherwise pointing them
  to the same file.

  "Unrecognized" files are local files whose sha1 matches no photo in the
  inventory: only the first 5 are printed, photo-unrecognized.py lists them
  all.

USAGE
  ./photo-fixdate.py /path/to/backup/proton-photos --dry-run
  ./photo-fixdate.py /path/to/backup/proton-photos                # dates only
  ./photo-fixdate.py /path/to/backup/proton-photos --reorganize

SAFETY
  Works locally only, never touches Proton. Never deletes files: at most it
  moves them between year folders.
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

import protonlib

INVENTORY = ".proton-photo-inventory.json"
MAP = ".proton-photo-map.json"


def parse_args():
    p = argparse.ArgumentParser(description="Restore capture dates on photos.")
    p.add_argument("dest", help="photo backup folder")
    p.add_argument("--reorganize", action="store_true",
                   help="move photos into the folder of their capture year")
    p.add_argument("--dry-run", action="store_true", help="show without changing anything")
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    return a


def capture_time(x):
    """Capture date, with fallbacks in order of reliability."""
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


def to_dt(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    a = parse_args()
    inv_path = os.path.join(a.dest, INVENTORY)
    if not os.path.exists(inv_path):
        sys.exit(f"ERROR: {inv_path} not found. Run photo-backup.py first.")

    with open(inv_path, encoding="utf-8") as f:
        inv = json.load(f)

    # several distinct photos on Proton can have identical content (the same
    # picture uploaded from two phones). The index must keep all of them: with
    # a single entry per hash, the other uids look absent from the backup
    # while they're there, and end up being downloaded again for nothing.
    by_sha = {}
    no_hash = 0
    for x in inv:
        rev = x.get("activeRevision") or {}
        h = (rev.get("claimedDigests") or {}).get("sha1")
        if not h:
            no_hash += 1
            continue
        by_sha.setdefault(h, []).append(x)

    print("")
    print(f"  Backup    : {a.dest}")
    print(f"  Inventory : {len(inv)} photos ({len(inv) - no_hash} with sha1)")
    if no_hash:
        print(f"  No sha1   : {no_hash} (can't be matched by content)")
    print("")
    print("  Hashing local files...")

    album_dir = os.path.join(a.dest, "album")
    files = []
    for dp, dn, fn in os.walk(a.dest):
        if dp.startswith(album_dir):
            continue
        for f in fn:
            if f.startswith(".") or f.endswith(".txt") or f.endswith(".json"):
                continue
            files.append(os.path.join(dp, f))

    # local files grouped by content
    files_by_sha = {}
    unrecognized = []
    t0 = time.time()
    for i, p in enumerate(files, 1):
        h = protonlib.sha1_of(p)
        if h and h in by_sha:
            files_by_sha.setdefault(h, []).append(p)
        else:
            unrecognized.append(p)
        if i % 250 == 0:
            el = int(time.time() - t0)
            sys.stderr.write(f"\r  {i}/{len(files)} files | {el}s      ")
            sys.stderr.flush()
    sys.stderr.write("\r" + " " * 50 + "\r")

    # pair uids with files: when several photos share the same content, each
    # gets its own copy if there is one, otherwise they all point to the same
    # file (the content is identical)
    photo_map = {}
    shared_content = 0
    for h, records in by_sha.items():
        paths = files_by_sha.get(h) or []
        if not paths:
            continue
        if len(records) > 1:
            shared_content += len(records) - 1
        for n, x in enumerate(records):
            photo_map[x.get("uid") or x.get("nodeUid")] = paths[min(n, len(paths) - 1)]

    print(f"  Local files          : {len(files)}")
    print(f"  Matched by sha1      : {len(photo_map)}")
    if shared_content:
        print(f"  Photos sharing content with others: {shared_content}")
    print(f"  Unrecognized         : {len(unrecognized)}")
    for p in unrecognized[:5]:
        print("     " + os.path.basename(p))

    # --- dates and reorganization ------------------------------------------
    dates_ok = dates_ko = moved = 0
    examples = []
    by_uid = {(x.get("uid") or x.get("nodeUid")): x for x in inv}

    for uid, path in photo_map.items():
        x = by_uid.get(uid)
        if not x:
            continue
        dt = to_dt(capture_time(x))
        if not dt:
            dates_ko += 1
            continue

        new_path = path
        if a.reorganize:
            year = str(dt.astimezone(timezone.utc).year)
            expected = os.path.join(a.dest, year)
            if os.path.dirname(path) != expected:
                if not a.dry_run:
                    os.makedirs(expected, exist_ok=True)
                    cand = os.path.join(expected, os.path.basename(path))
                    n = 1
                    base, ext = os.path.splitext(cand)
                    while os.path.exists(cand):
                        cand = f"{base} ({n}){ext}"
                        n += 1
                    shutil.move(path, cand)
                    new_path = cand
                moved += 1
                if len(examples) < 5:
                    examples.append((os.path.basename(path),
                                     os.path.basename(os.path.dirname(path)), year))

        if not a.dry_run:
            ts = dt.timestamp()
            try:
                os.utime(new_path, (ts, ts))
                dates_ok += 1
            except OSError:
                dates_ko += 1
        else:
            dates_ok += 1
        photo_map[uid] = new_path

    print("")
    print(f"  Dates set            : {dates_ok}")
    if dates_ko:
        print(f"  Dates not set        : {dates_ko}")
    if a.reorganize:
        print(f"  Photos to move       : {moved}")
        for name, src, dst in examples:
            print(f"     {name}: {src} -> {dst}")

    if a.dry_run:
        print("")
        print("  (--dry-run: nothing changed)")
        return 0

    p = os.path.join(a.dest, MAP)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(photo_map, f, ensure_ascii=False, indent=1)
    print("")
    print(f"  uid -> file map saved: {p}")
    print("  (needed by album-backup.py, photo-prune.py and photo-redate.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
