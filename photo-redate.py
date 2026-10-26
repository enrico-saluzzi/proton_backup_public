#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-redate.py - Fix the date of scanned photos on Proton, using the albums.

WHY
  Prints photographed or scanned years later have a capture date that is
  technically right (when the button was pressed) but semantically wrong: on
  Proton's timeline they land in the year of the scan, not of the event. A
  2003 holiday shows up in 2019. The right information lives in the albums,
  which you built by hand.

TWO STEPS
  1. PLAN (default): for every album whose name starts with a year
     ("2003 - Holiday in Rome"), compare that year with the capture date of
     each photo and plan a correction for those that don't match. The new
     date takes the day and month of the MOST RECENT photo of the album that
     already has the right year, so the corrected photos group on the
     timeline next to the others of the same event (January 1st if none has
     the right year). The plan is printed and saved as redate-plan.json.
     Nothing is changed. Albums that start with a year but deliberately hold
     photos from other years (e.g. "2023 - Christmas" with old family
     photos) can be left out with --exclude or --exclude-file.

  2. --apply: applies the SAVED plan, i.e. the one you reviewed. Proton
     places photos using DateTimeOriginal but has no command to edit the
     metadata of a photo already uploaded, so for each photo it:
       0. reads the timeline BEFORE uploading, to recognize the new uploads
          by difference (sha1 alone can't: photos may share the same content)
       1. copies the local file into a work folder and fixes its EXIF dates
          with exiftool
       2. uploads the corrected copy (`photo upload -c rename`)
       3. reads the timeline again and finds each new uid by sha1, among the
          photos that weren't there before
       4. adds the new photo back to its album
       5. moves the old version to Proton's trash
       6. restores the original file name on the new copy (--skip-rename
          to skip)
     The original is trashed ONLY after the new copy has been uploaded,
     found and put back in its album; Proton's trash has no expiry. Every
     step is saved in reupload-state.json: an interruption neither loses
     work nor creates duplicates, just run --apply again.

  Needs a photo backup made with photo-backup.py --albums (timeline, album
  list and the uid -> file map), and exiftool for --apply
  (brew install exiftool).

LIMITS
  - Videos are skipped: Proton ignores the QuickTime date tags and places a
    re-uploaded video at its upload date. --include-video tries anyway.
  - Editing an album moves it to the top of the album list on Proton, and
    there's no way to change that order.
  - The CLI exits 0 even when an upload does nothing: that's why the new
    photo is looked for on the timeline instead of trusting the exit code.

USAGE
  ./photo-redate.py /path/to/backup/proton-photos                      # plan
  ./photo-redate.py /path/to/backup/proton-photos --exclude-file excluded.txt
  ./photo-redate.py /path/to/backup/proton-photos --apply --limit 5    # try a few
  ./photo-redate.py /path/to/backup/proton-photos --apply

  Afterwards, run photo-backup.py --albums again to bring the backup in line.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter

import protonlib

ALBUM_MAP = ".proton-albums.json"
INVENTORY = ".proton-photo-inventory.json"
PHOTO_MAP = ".proton-photo-map.json"
PLAN = "redate-plan.json"
STATE = "reupload-state.json"
VIDEO_EXT = (".mov", ".mp4", ".m4v", ".avi", ".3gp")


def parse_args():
    p = argparse.ArgumentParser(
        description="Fix the date of scanned photos on Proton, using the albums.")
    p.add_argument("dest", help="photo backup folder")
    p.add_argument("--exclude", default="",
                   help="album names to leave alone, comma-separated")
    p.add_argument("--exclude-file", default=None,
                   help="file with one album name per line (# for comments)")
    p.add_argument("--max-print", type=int, default=40)
    p.add_argument("--apply", action="store_true",
                   help="apply the saved plan on Proton (re-upload, re-add, trash)")
    p.add_argument("--limit", type=int, default=0, help="with --apply: handle at most N photos")
    p.add_argument("--include-video", action="store_true",
                   help="with --apply: try videos too (Proton ignores their dates)")
    p.add_argument("--skip-rename", action="store_true",
                   help="with --apply: don't restore the original name on the new copy")
    p.add_argument("--wait", type=int, default=20,
                   help="with --apply: seconds to wait before looking for the uploads "
                        "(default 20)")
    p.add_argument("--work", default=None,
                   help="with --apply: work folder (default: _reupload in the backup folder)")
    p.add_argument("--exiftool", default="exiftool")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI)
    p.add_argument("--timeout", type=int, default=600)
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    a.work = os.path.expanduser(a.work) if a.work else os.path.join(a.dest, "_reupload")
    excluded = set(x.strip() for x in a.exclude.split(",") if x.strip())
    if a.exclude_file and os.path.exists(os.path.expanduser(a.exclude_file)):
        with open(os.path.expanduser(a.exclude_file), encoding="utf-8") as f:
            excluded |= set(l.strip() for l in f if l.strip() and not l.startswith("#"))
    a.excluded = excluded
    return a


def load_json(a, name, required_by):
    path = os.path.join(a.dest, name)
    if not os.path.exists(path):
        sys.exit(f"ERROR: {name} is missing. Run {required_by} first.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_video(name):
    return (name or "").lower().endswith(VIDEO_EXT)


# --- 1. plan -----------------------------------------------------------------

def make_plan(a):
    albums = load_json(a, ALBUM_MAP, "photo-backup.py --albums")
    inv = {(x.get("uid") or x.get("nodeUid")): x
           for x in load_json(a, INVENTORY, "photo-backup.py")}
    photo_map = load_json(a, PHOTO_MAP, "photo-backup.py")

    plan, skipped, no_reference = [], [], []
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
            (inside if ts[:4] == target else outside).append((u, ts, x))
        if not outside:
            continue
        # day/month of the most recent photo that already has the right year
        if inside:
            ref = max(inside, key=lambda t: t[1])[1]
            month_day, time_of_day = ref[5:10], ref[11:19] or "12:00:00"
        else:
            month_day, time_of_day = "01-01", "12:00:00"
            no_reference.append(name)
        for u, ts, x in outside:
            plan.append({
                "uid": u, "album": name,
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
    videos = [p for p in plan if is_video(p["file_name"])]
    if videos:
        print(f"  Videos               : {len(videos)} (skipped by --apply, see LIMITS)")
    if no_reference:
        print("  Albums with no photo of the right year (January 1st is used):")
        for s in no_reference:
            print(f"     {s}")

    print("")
    print(f"{'ALBUM':<44} {'PHOTOS':>6}  NEW DATE")
    print("-" * 78)
    for name, n in sorted(Counter(p["album"] for p in plan).items()):
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
    print("  NOTHING was changed. Review the plan, then run again with --apply.")
    return 0


# --- 2. apply ----------------------------------------------------------------

def load_state(a):
    p = os.path.join(a.dest, STATE)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(a, st):
    p = os.path.join(a.dest, STATE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def cli(a, cmd, timeout=None):
    return protonlib.run_cli(a.cli, cmd, timeout or a.timeout)


def apply_plan(a):
    plan = load_json(a, PLAN, "photo-redate.py (without --apply) to make the plan")
    albums = load_json(a, ALBUM_MAP, "photo-backup.py --albums")
    album_uid = {x["name"]: x["uid"] for x in albums}

    videos = [p for p in plan if is_video(p.get("file_name"))]
    if not a.include_video:
        plan = [p for p in plan if not is_video(p.get("file_name"))]
    st = load_state(a)
    todo = [p for p in plan if st.get(p["uid"], {}).get("phase") != "done"]
    if a.limit:
        todo = todo[:a.limit]

    print("")
    print(f"  Plan        : {len(plan)} photos"
          + (f" ({len(videos)} videos skipped)" if videos and not a.include_video else ""))
    print(f"  Already done: {sum(1 for v in st.values() if v.get('phase') == 'done')}")
    print(f"  To handle   : {len(todo)}")
    print(f"  Work folder : {a.work}")
    print("")
    if not todo:
        print("  Nothing to do.")
        return 0
    os.makedirs(a.work, exist_ok=True)

    # phase 0: the timeline BEFORE the uploads, to recognize new uids by difference
    print("  PHASE 0: reading the timeline before uploading")
    r = cli(a, ["photo", "timeline", "-j"], timeout=3600)
    if r.returncode != 0:
        sys.exit(f"ERROR in the initial read: {(r.stderr or r.stdout)[:200]}")
    uids_before = {x.get("nodeUid") or x.get("uid") for x in json.loads(r.stdout)}
    print(f"  {len(uids_before)} photos already present")

    # phases 1-2: prepare and upload
    print("")
    print("  PHASE 1-2: EXIF correction and upload")
    uploaded = []
    for i, p in enumerate(todo, 1):
        u = p["uid"]
        rec = st.setdefault(u, {})
        src = p.get("local_file")
        if not src or not os.path.exists(src):
            rec.update(phase="error", error="local file missing")
            continue
        dst = os.path.join(a.work, os.path.basename(src))
        try:
            if rec.get("phase") not in ("prepared", "uploaded"):
                shutil.copy2(src, dst)
                tags = ["-overwrite_original", f"-AllDates={p['exif']}"]
                if is_video(p["file_name"]):
                    tags += [f"-QuickTime:CreateDate={p['exif']}",
                             f"-QuickTime:ModifyDate={p['exif']}"]
                r = subprocess.run([a.exiftool] + tags + [dst], capture_output=True, text=True)
                if r.returncode != 0:
                    rec.update(phase="error", error=(r.stderr or "")[:200])
                    continue
                rec.update(phase="prepared", file=dst, sha1=protonlib.sha1_of(dst))
                save_state(a, st)
            if rec.get("phase") == "prepared":
                r = cli(a, ["photo", "upload", "-c", "rename", rec["file"]])
                output = ((r.stdout or "") + (r.stderr or "")).lower()
                # the CLI exits 0 even when it uploads nothing
                if "conflict" in output or "strategy" in output:
                    rec.update(phase="error", error="unresolved upload conflict")
                    save_state(a, st)
                    continue
                if r.returncode != 0:
                    rec.update(phase="error", error=(r.stderr or r.stdout or "")[:200])
                    save_state(a, st)
                    continue
                rec.update(phase="uploaded")
                save_state(a, st)
            uploaded.append(u)
        except Exception as e:
            rec.update(phase="error", error=str(e)[:200])
            save_state(a, st)
        sys.stderr.write(f"\r  {i}/{len(todo)} prepared and uploaded    ")
        sys.stderr.flush()
    sys.stderr.write("\n")
    errs = [u for u, v in st.items() if v.get("phase") == "error"]
    print(f"  Uploaded: {len(uploaded)}   errors: {len(errs)}")
    if not uploaded:
        print("  No upload succeeded: stopping.")
        return 1

    # phase 3: find the new uids by sha1, among the photos that weren't there
    print("")
    if a.wait:
        print(f"  waiting {a.wait}s for Proton to index the uploads...")
        time.sleep(a.wait)
    print("  PHASE 3: reading the timeline again to find the new photos")
    r = cli(a, ["photo", "timeline", "-d", "-j"], timeout=3600)
    if r.returncode != 0:
        print("  ERROR while re-reading: the photos are uploaded but not matched.")
        print("  Run --apply again: it will resume from here.")
        return 1
    appeared = {}
    for x in json.loads(r.stdout):
        u2 = x.get("uid") or x.get("nodeUid")
        if u2 in uids_before:
            continue
        h = ((x.get("activeRevision") or {}).get("claimedDigests") or {}).get("sha1")
        if h:
            appeared.setdefault(h, []).append(u2)
    if not appeared:
        print("  WARNING: no new photo on the timeline. The uploads didn't go")
        print("  through, or Proton hasn't indexed them yet. Nothing was trashed.")
        print("  Run --apply again with a larger --wait.")
    found, assigned = 0, set()
    for u in uploaded:
        rec = st[u]
        cand = [c for c in (appeared.get(rec.get("sha1")) or []) if c not in assigned]
        if cand:
            rec.update(new_uid=cand[0], phase="found")
            assigned.add(cand[0])
            found += 1
        else:
            rec.update(phase="not_found", error="no new uid with this content")
    save_state(a, st)
    print(f"  New photos found: {found}/{len(uploaded)}")

    # phase 4: back into the albums
    print("")
    print("  PHASE 4: adding the photos back to their albums")
    in_album = 0
    for p in todo:
        rec = st.get(p["uid"], {})
        if rec.get("phase") != "found":
            continue
        auid = album_uid.get(p["album"])
        if not auid:
            rec["error"] = "album not found"
            continue
        r = cli(a, ["album", "add-photo", f"/albums/{auid}", f"/photos/{rec['new_uid']}"])
        if r.returncode == 0:
            rec["phase"] = "in_album"
            in_album += 1
        else:
            rec["error"] = (r.stderr or r.stdout or "")[:200]
        save_state(a, st)
    print(f"  Added back: {in_album}")

    # phase 5: trash the old versions
    print("")
    print("  PHASE 5: trashing the old versions")
    trashed = 0
    for p in todo:
        rec = st.get(p["uid"], {})
        if rec.get("phase") != "in_album":
            continue
        r = cli(a, ["filesystem", "trash", f"/photos/{p['uid']}"])
        if r.returncode == 0:
            rec["phase"] = "done"
            trashed += 1
        else:
            rec["error"] = (r.stderr or r.stdout or "")[:200]
        save_state(a, st)
    print(f"  Trashed: {trashed}")

    # phase 6: with -c rename the new copy got an altered name, because the
    # old one still existed; now that it's trashed, the name is free again
    print("")
    if a.skip_rename:
        print("  PHASE 6: skipped (--skip-rename)")
    else:
        print("  PHASE 6: restoring the original names")
        renamed = 0
        for p in todo:
            rec = st.get(p["uid"], {})
            if rec.get("phase") != "done" or rec.get("renamed") or not p.get("file_name"):
                continue
            r = cli(a, ["filesystem", "rename", f"/photos/{rec['new_uid']}", p["file_name"]])
            if r.returncode == 0:
                rec["renamed"] = True
                renamed += 1
            else:
                rec["rename_error"] = (r.stderr or r.stdout or "")[:150]
            save_state(a, st)
        print(f"  Renamed: {renamed}")

    print("")
    print("=" * 60)
    for k, n in sorted(Counter(v.get("phase", "?") for v in st.values()).items()):
        print(f"  {n:>5}  {k}")
    print("=" * 60)
    problems = [(u, v) for u, v in st.items() if v.get("phase") != "done" and v.get("error")]
    if problems:
        print("")
        print("  To review:")
        for u, v in problems[:10]:
            print(f"    {u[-12:]}  {v.get('phase')}  {v.get('error', '')[:70]}")
    print("")
    print("  The old versions are in Proton's trash, recoverable.")
    print("  Now bring the backup in line:  ./photo-backup.py DEST --albums ...")
    return 0


def main():
    a = parse_args()
    return apply_plan(a) if a.apply else make_plan(a)


if __name__ == "__main__":
    sys.exit(main())
