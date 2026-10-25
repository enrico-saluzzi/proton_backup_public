#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-redate.py - Plan the date correction of scanned photos, based on albums.

WHY
  Prints photographed or scanned years later have a capture date that is
  technically right (when the button was pressed) but semantically wrong: on
  Proton's timeline they land in the year of the scan, not of the event. A
  2003 holiday shows up in 2019. The right information lives in the albums,
  which you built by hand.

WHAT IT DOES
  For every album whose name starts with a year ("2003 - Holiday in Rome"),
  it compares that year with the capture date of each photo and proposes a
  correction for the photos that don't match. As the new date it uses the
  day and month of the MOST RECENT photo of the album that already has the
  right year, so the corrected photos group on the timeline next to the
  others of the same event. If no photo in the album has the right year, it
  uses January 1st.

  The plan is saved as redate-plan.json in the backup folder and applied by
  photo-reupload.py. Review it first.

  Albums whose name starts with a year but whose photos are deliberately
  from other years (e.g. "2023 - Christmas" with old family photos) can be
  excluded with --exclude or --exclude-file.

  Requires .proton-albums.json (album-backup.py) and
  .proton-photo-inventory.json (photo-backup.py); .proton-photo-map.json
  (photo-fixdate.py) is needed to find the local file to fix.

USAGE
  ./photo-redate.py /path/to/backup/proton-photos
  ./photo-redate.py /path/to/backup/proton-photos --exclude "2012 - New Year,2023 - Christmas"
  ./photo-redate.py /path/to/backup/proton-photos --exclude-file excluded.txt

SAFETY
  CHANGES NOTHING. It produces a plan to review before applying it.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter

ALBUM_MAP = ".proton-albums.json"
INVENTORY = ".proton-photo-inventory.json"
PHOTO_MAP = ".proton-photo-map.json"
PLAN = "redate-plan.json"


def parse_args():
    p = argparse.ArgumentParser(
        description="Plan the date correction of photos based on albums.")
    p.add_argument("dest", help="photo backup folder")
    p.add_argument("--exclude", default="",
                   help="album names to leave alone, comma-separated")
    p.add_argument("--exclude-file", default=None,
                   help="file with one album name per line (# for comments)")
    p.add_argument("--max-print", type=int, default=40)
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    excluded = set(x.strip() for x in a.exclude.split(",") if x.strip())
    if a.exclude_file and os.path.exists(os.path.expanduser(a.exclude_file)):
        with open(os.path.expanduser(a.exclude_file), encoding="utf-8") as f:
            excluded |= set(l.strip() for l in f if l.strip()
                            and not l.startswith("#"))
    a.excluded = excluded
    return a


def year(ts):
    return (ts or "")[:4]


def main():
    a = parse_args()
    for n in (ALBUM_MAP, INVENTORY):
        if not os.path.exists(os.path.join(a.dest, n)):
            sys.exit(f"ERROR: {n} is missing. Run album-backup.py and photo-fixdate.py first.")

    with open(os.path.join(a.dest, ALBUM_MAP), encoding="utf-8") as f:
        albums = json.load(f)
    with open(os.path.join(a.dest, INVENTORY), encoding="utf-8") as f:
        inv = {(x.get("uid") or x.get("nodeUid")): x for x in json.load(f)}
    photo_map = {}
    mp = os.path.join(a.dest, PHOTO_MAP)
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            photo_map = json.load(f)

    plan = []
    skipped = []
    no_reference = []

    for al in albums:
        name = al["name"]
        m = re.match(r"^\s*(19\d{2}|20\d{2})\b", name)
        if not m:
            continue
        if name in a.excluded:
            skipped.append(name)
            continue
        target = m.group(1)

        inside, outside = [], []
        for u in al["photos"]:
            x = inv.get(u)
            if not x:
                continue
            ts = (x.get("photo") or {}).get("captureTime") or ""
            (inside if year(ts) == target else outside).append((u, ts, x))
        if not outside:
            continue

        # day/month of the most recent photo that already has the right year
        if inside:
            ref = max(inside, key=lambda t: t[1])[1]
            month_day = ref[5:10]          # MM-DD
            time_of_day = ref[11:19] or "12:00:00"
        else:
            month_day, time_of_day = "01-01", "12:00:00"
            no_reference.append(name)

        for u, ts, x in outside:
            plan.append({
                "uid": u,
                "album": name,
                "file_name": (x.get("name") or {}).get("value"),
                "current_date": ts,
                "new_date": f"{target}-{month_day}T{time_of_day}Z",
                "exif": f"{target}:{month_day.replace('-', ':')} {time_of_day}",
                "local_file": photo_map.get(u),
            })

    print("")
    print(f"  Albums examined      : {len(albums)}")
    print(f"  Albums excluded      : {len(skipped)}")
    for s in skipped:
        print(f"     {s}")
    print(f"  Photos to redate     : {len(plan)}")
    no_file = [p for p in plan if not p["local_file"]]
    if no_file:
        print(f"  Without a local file : {len(no_file)} (can't be fixed)")
    if no_reference:
        print("  Albums with no photo of the right year (January 1st is used):")
        for s in no_reference:
            print(f"     {s}")

    print("")
    per_album = Counter(p["album"] for p in plan)
    print(f"{'ALBUM':<44} {'PHOTOS':>6}  NEW DATE")
    print("-" * 78)
    for name, n in sorted(per_album.items()):
        d = next(p["new_date"][:10] for p in plan if p["album"] == name)
        print(f"{name[:44]:<44} {n:>6}  {d}")

    print("")
    print("EXAMPLES (first rows of the plan)")
    print("-" * 78)
    for p in plan[:a.max_print]:
        print(f"  {p['current_date'][:10]} -> {p['new_date'][:10]}  "
              f"{(p['file_name'] or '?')[:34]:<34} [{p['album'][:24]}]")
    if len(plan) > a.max_print:
        print(f"  ... and {len(plan) - a.max_print} more")

    out = os.path.join(a.dest, PLAN)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    print("")
    print(f"  Plan saved: {out}")
    print("  NOTHING was changed. Review the plan before applying it with")
    print("  photo-reupload.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
