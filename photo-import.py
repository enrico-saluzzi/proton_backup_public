#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-import.py - Add to the backup photos downloaded by hand from the web.

WHY
  Some photos can't be downloaded through the CLI (typically the versions
  derived from iOS edits, with `_modified` in the name), but they are
  reachable from the web interface. After downloading them by hand, this
  script puts them in the backup as if photo-backup.py had fetched them, so
  the incremental backup stops retrying them on every run.

WHAT IT DOES
  For each file in the source folder whose name matches a photo that is not
  yet in the backup:
  1. moves it (or copies it, with --copy) into the folder of its capture year
  2. sets the capture date as the file's modification time
  3. records its uid in the backup state file (.proton-photo-state.txt)
  4. removes it from photos-not-downloaded.txt

  Matching is by NAME, case-insensitive (the browser may save
  IMG_2808_modified.MOV where the inventory says .mov). When possible the
  match is checked against the sha1 declared by Proton: a mismatch is
  reported, because the browser may convert the format while downloading.
  Names that match more than one photo are skipped and reported.

USAGE
  ./photo-import.py /path/to/backup/proton-photos ~/Downloads --dry-run
  ./photo-import.py /path/to/backup/proton-photos ~/Downloads

SAFETY
  Moves files from the source folder into the backup. Never touches Proton.
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

import protonlib

INVENTORY = ".proton-photo-inventory.json"
STATE = ".proton-photo-state.txt"
FAILED = "photos-not-downloaded.txt"


def parse_args():
    p = argparse.ArgumentParser(
        description="Record in the backup photos downloaded by hand.")
    p.add_argument("dest", help="photo backup folder")
    p.add_argument("source", help="folder where you downloaded the files (e.g. ~/Downloads)")
    p.add_argument("--copy", action="store_true", help="copy instead of moving")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    a.source = os.path.expanduser(a.source)
    return a


def capture_time(x):
    ph = x.get("photo") or {}
    if ph.get("captureTime"):
        return ph["captureTime"]
    rev = x.get("activeRevision") or {}
    m = rev.get("claimedAdditionalMetadata") or {}
    if (m.get("Camera") or {}).get("CaptureTime"):
        return m["Camera"]["CaptureTime"]
    return x.get("captureTime") or rev.get("claimedModificationTime")


def main():
    a = parse_args()
    inv_path = os.path.join(a.dest, INVENTORY)
    if not os.path.exists(inv_path):
        sys.exit(f"ERROR: {inv_path} not found. Run photo-backup.py first.")
    if not os.path.isdir(a.source):
        sys.exit(f"ERROR: {a.source} doesn't exist.")

    with open(inv_path, encoding="utf-8") as f:
        inv = json.load(f)

    state_path = os.path.join(a.dest, STATE)
    already = set()
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            already = set(l.strip() for l in f if l.strip())

    # index by name, limited to the photos not yet in the backup
    by_name = {}
    for x in inv:
        u = x.get("uid") or x.get("nodeUid")
        if not u or u in already:
            continue
        name = (x.get("name") or {}).get("value")
        if name:
            # case-insensitive: the browser may save IMG_2808_modified.MOV
            # where the inventory says .mov
            by_name.setdefault(name.lower(), []).append(x)

    candidates = [f for f in os.listdir(a.source)
                  if not f.startswith(".") and os.path.isfile(os.path.join(a.source, f))]

    print("")
    print(f"  Backup  : {a.dest}")
    print(f"  Source  : {a.source}  ({len(candidates)} files)")
    print(f"  Missing from the backup: {sum(len(v) for v in by_name.values())}")
    print("")

    imported, ignored, ambiguous, hash_mismatch = 0, [], 0, 0
    new_uids = []

    for f in sorted(candidates):
        rec = by_name.get(f.lower())
        if not rec:
            ignored.append(f)
            continue
        if len(rec) > 1:
            print(f"  AMBIGUOUS (several photos with this name): {f}")
            ambiguous += 1
            continue
        x = rec[0]
        u = x.get("uid") or x.get("nodeUid")
        src = os.path.join(a.source, f)

        expected = ((x.get("activeRevision") or {}).get("claimedDigests") or {}).get("sha1")
        note = ""
        if expected:
            if protonlib.sha1_of(src) != expected:
                note = "  [different hash: the browser may have converted the file]"
                hash_mismatch += 1

        dt = None
        ts = capture_time(x)
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                dt = None
        year = str(dt.astimezone(timezone.utc).year) if dt else "no-date"
        target_dir = os.path.join(a.dest, year)
        target = os.path.join(target_dir, f)

        print(f"  {f}  ->  {year}/{note}")
        if not a.dry_run:
            os.makedirs(target_dir, exist_ok=True)
            base, ext = os.path.splitext(target)
            n = 1
            while os.path.exists(target):
                target = f"{base} ({n}){ext}"
                n += 1
            if a.copy:
                shutil.copy2(src, target)
            else:
                shutil.move(src, target)
            if dt:
                os.utime(target, (dt.timestamp(), dt.timestamp()))
            new_uids.append(u)
        imported += 1

    print("")
    print(f"  Imported      : {imported}")
    if ambiguous:
        print(f"  Ambiguous     : {ambiguous} (name used more than once: rename them by hand)")
    if hash_mismatch:
        print(f"  Different hash: {hash_mismatch} (content probably converted by the browser)")
    if ignored:
        print(f"  Ignored       : {len(ignored)} files not expected in the backup")
        for f in ignored[:5]:
            print("     " + f)

    if a.dry_run:
        print("")
        print("  (--dry-run: no file moved)")
        return 0

    if new_uids:
        with open(state_path, "a", encoding="utf-8") as f:
            f.write("\n".join(new_uids) + "\n")
        print("")
        print(f"  {len(new_uids)} uids recorded: they won't be retried anymore.")

        # clean up the list of failed downloads
        fp = os.path.join(a.dest, FAILED)
        if os.path.exists(fp):
            imported_set = set(new_uids)
            with open(fp, encoding="utf-8") as f:
                rows = [l for l in f if l.strip()
                        and l.split("\t")[0].strip() not in imported_set]
            with open(fp, "w", encoding="utf-8") as f:
                f.writelines(rows)
            print(f"  {len(rows)} photos still to recover.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
