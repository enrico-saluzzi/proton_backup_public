#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-reupload.py - Apply the date-correction plan to Proton Photos.

WHY
  Proton places photos on the timeline using DateTimeOriginal (verified with
  a three-date test on different EXIF fields), but offers no command to edit
  the metadata of a photo that is already uploaded. The only way is to fix
  the EXIF locally, upload the corrected copy, put it back in its album and
  trash the original.

PHASES (each one resumable: state is saved after every step in
reupload-state.json, so an interruption neither loses work nor creates
duplicates)

  0. SNAPSHOT  reads the timeline BEFORE uploading, to recognize new uploads
               by difference (sha1 alone can't: many photos may share the
               same content)
  1. PREPARE   copies the local file into a work folder and fixes its EXIF
               dates with exiftool
  2. UPLOAD    uploads the corrected copy (`photo upload -c <strategy>`)
  3. MATCH     reads the timeline again and matches each new uid by sha1,
               among the photos that weren't there before
  4. ALBUM     adds the new photo back to the album it came from
  5. TRASH     moves the old version to Proton's trash
  6. RENAME    restores the original file name on the new copy (with
               `-c rename` the CLI gives it an altered name, because the old
               copy still exists at upload time). Skip with --skip-rename.

  The original is trashed ONLY after the new copy has been uploaded, found
  and put back in its album. Proton's trash has no expiry, so every step
  stays reversible.

  Input: redate-plan.json, produced by photo-redate.py.
  Requires exiftool (brew install exiftool).

LIMITS
  - Doesn't work for videos: Proton ignores the QuickTime date tags and
    places the video at its upload date. Use --skip-video.
  - Editing an album moves it to the top of the album list on Proton, and
    there's no way to change that order.
  - The CLI exits 0 even when an upload does nothing (without -c it asks for
    confirmation on stdin and gives up): that's why the output is checked
    and the new uid is looked for on the timeline.

USAGE
  ./photo-reupload.py /path/to/backup/proton-photos --dry-run
  ./photo-reupload.py /path/to/backup/proton-photos --apply --limit 5     # try a few
  ./photo-reupload.py /path/to/backup/proton-photos --apply --skip-video

  After checking, regenerate inventory, map and albums:
      ./photo-backup.py DEST --details
      ./photo-fixdate.py DEST --reorganize
      ./album-backup.py DEST --album-dir ...
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import protonlib

PLAN = "redate-plan.json"
STATE = "reupload-state.json"
ALBUM_MAP = ".proton-albums.json"
VIDEO_EXT = (".mov", ".mp4", ".m4v", ".avi", ".3gp")


def parse_args():
    p = argparse.ArgumentParser(description="Apply the date-correction plan on Proton.")
    p.add_argument("dest", help="photo backup folder")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI)
    p.add_argument("--exiftool", default="exiftool")
    p.add_argument("--work", default=None, help="work folder (default: dest/_reupload)")
    p.add_argument("--limit", type=int, default=0, help="handle at most N photos")
    p.add_argument("--only-video", action="store_true", help="handle videos only")
    p.add_argument("--skip-video", action="store_true", help="leave videos out")
    p.add_argument("--apply", action="store_true", help="actually do it")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--conflict", default="rename",
                   choices=("rename", "skip", "remove"),
                   help="strategy if a photo with the same name already exists "
                        "(default rename)")
    p.add_argument("--skip-rename", action="store_true",
                   help="don't restore the original name on the new copy")
    p.add_argument("--wait", type=int, default=20,
                   help="seconds to wait between uploads and re-reading (default 20)")
    p.add_argument("--timeout", type=int, default=600)
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    a.work = os.path.expanduser(a.work) if a.work else os.path.join(a.dest, "_reupload")
    return a


def cli(a, cmd, timeout=None):
    return protonlib.run_cli(a.cli, cmd, timeout or a.timeout)


def is_video(name):
    return (name or "").lower().endswith(VIDEO_EXT)


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


def main():
    a = parse_args()
    plan_path = os.path.join(a.dest, PLAN)
    if not os.path.exists(plan_path):
        sys.exit(f"ERROR: {plan_path} is missing. Run photo-redate.py first.")
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    with open(os.path.join(a.dest, ALBUM_MAP), encoding="utf-8") as f:
        albums = json.load(f)
    album_uid = {x["name"]: x["uid"] for x in albums}

    if a.only_video:
        plan = [p for p in plan if is_video(p.get("file_name"))]
    elif a.skip_video:
        plan = [p for p in plan if not is_video(p.get("file_name"))]

    st = load_state(a)
    todo = [p for p in plan if st.get(p["uid"], {}).get("phase") != "done"]
    if a.limit:
        todo = todo[:a.limit]

    print("")
    print(f"  Plan       : {len(plan)} photos")
    print(f"  Already done: {sum(1 for v in st.values() if v.get('phase') == 'done')}")
    print(f"  To handle  : {len(todo)}")
    print(f"  Work folder: {a.work}")
    print("")

    if not todo:
        print("  Nothing to do.")
        return 0

    if not a.apply or a.dry_run:
        for p in todo[:10]:
            print(f"  {p['current_date'][:10]} -> {p['new_date'][:10]}  "
                  f"{p['file_name']}  [{p['album']}]")
        if len(todo) > 10:
            print(f"  ... and {len(todo) - 10} more")
        print("")
        print("  (nothing changed: add --apply to execute)")
        return 0

    os.makedirs(a.work, exist_ok=True)

    # --- PHASE 0: snapshot of the timeline BEFORE the uploads ----------------
    # Used to recognize new uids by difference. sha1 alone isn't enough: when
    # many photos share identical content, searching by hash can return a
    # pre-existing photo instead of the one just uploaded.
    print("  PHASE 0: reading the timeline before uploading")
    r = cli(a, ["photo", "timeline", "-j"], timeout=3600)
    if r.returncode != 0:
        sys.exit(f"ERROR in the initial read: {(r.stderr or r.stdout)[:200]}")
    uids_before = {x.get("nodeUid") or x.get("uid") for x in json.loads(r.stdout)}
    print(f"  {len(uids_before)} photos already present")
    print("")

    # --- PHASE 1-2: prepare and upload ---------------------------------------
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
                r = subprocess.run([a.exiftool] + tags + [dst],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    rec.update(phase="error", error=(r.stderr or "")[:200])
                    continue
                rec.update(phase="prepared", file=dst, sha1=protonlib.sha1_of(dst))
                save_state(a, st)

            if rec.get("phase") == "prepared":
                r = cli(a, ["photo", "upload", "-c", a.conflict, rec["file"]])
                output = ((r.stdout or "") + (r.stderr or "")).lower()
                # the CLI exits 0 even when it uploads nothing: without -c it
                # asks for confirmation on stdin and, with stdin closed, gives up
                if "conflict" in output or "strategy" in output:
                    rec.update(phase="error",
                               error="unresolved conflict: use --conflict rename")
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

    # --- PHASE 3: match the new uids by sha1 ---------------------------------
    print("")
    if a.wait:
        print(f"  waiting {a.wait}s for Proton to index the uploads...")
        time.sleep(a.wait)
    print("  PHASE 3: reading the timeline again to match the new uids")
    r = cli(a, ["photo", "timeline", "-d", "-j"], timeout=3600)
    if r.returncode != 0:
        print("  ERROR while re-reading: the photos are uploaded but not matched.")
        print("  Rerun the script: it will resume from here.")
        return 1
    after = json.loads(r.stdout)
    # only what wasn't there before: for each sha1, the uids that appeared now
    appeared = {}
    for x in after:
        u2 = x.get("uid") or x.get("nodeUid")
        if u2 in uids_before:
            continue
        h = ((x.get("activeRevision") or {}).get("claimedDigests") or {}).get("sha1")
        if h:
            appeared.setdefault(h, []).append(u2)
    tot_appeared = sum(len(v) for v in appeared.values())
    print(f"  Photos that appeared after the uploads: {tot_appeared}")
    if tot_appeared == 0:
        print("")
        print("  WARNING: no new photo on the timeline.")
        print("  The uploads didn't go through, or Proton hasn't indexed them")
        print("  yet. Nothing was trashed.")
        print("  Check by hand:  proton-drive photo upload -c rename A_FILE")
        print("  and rerun with a larger --wait.")

    found = 0
    assigned = set()
    for u in uploaded:
        rec = st[u]
        cand = [c for c in (appeared.get(rec.get("sha1")) or [])
                if c not in assigned]
        if cand:
            rec["new_uid"] = cand[0]
            assigned.add(cand[0])
            rec["phase"] = "found"
            found += 1
        else:
            rec["phase"] = "not_found"
            rec["error"] = "no new uid with this content"
    save_state(a, st)
    print(f"  New uids matched: {found}/{len(uploaded)}")

    # --- PHASE 4: put back in albums ----------------------------------------
    print("")
    print("  PHASE 4: adding photos back to their albums")
    in_album = 0
    for p in todo:
        rec = st.get(p["uid"], {})
        if rec.get("phase") != "found":
            continue
        auid = album_uid.get(p["album"])
        if not auid:
            rec["error"] = "album not found"
            continue
        r = cli(a, ["album", "add-photo", f"/albums/{auid}",
                    f"/photos/{rec['new_uid']}"])
        if r.returncode == 0:
            rec["phase"] = "in_album"
            in_album += 1
        else:
            rec["error"] = (r.stderr or r.stdout or "")[:200]
        save_state(a, st)
    print(f"  Added back to albums: {in_album}")

    # --- PHASE 5: trash the old versions ------------------------------------
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

    # --- PHASE 6: restore the original names --------------------------------
    # With -c rename the CLI assigns an altered name, because the old copy
    # still exists at upload time. Once that copy is trashed, the original
    # name is free again.
    if a.skip_rename:
        print("")
        print("  PHASE 6: skipped (--skip-rename)")
    else:
        print("")
        print("  PHASE 6: restoring the original names")
        renamed = 0
        for p in todo:
            rec = st.get(p["uid"], {})
            if rec.get("phase") != "done" or rec.get("renamed"):
                continue
            name = p.get("file_name")
            if not name:
                continue
            r = cli(a, ["filesystem", "rename", f"/photos/{rec['new_uid']}", name])
            if r.returncode == 0:
                rec["renamed"] = True
                renamed += 1
            else:
                rec["rename_error"] = (r.stderr or r.stdout or "")[:150]
            save_state(a, st)
        print(f"  Renamed: {renamed}")

    # --- summary -------------------------------------------------------------
    print("")
    print("=" * 60)
    count = {}
    for v in st.values():
        count[v.get("phase", "?")] = count.get(v.get("phase", "?"), 0) + 1
    for k in sorted(count):
        print(f"  {count[k]:>5}  {k}")
    print("=" * 60)
    problems = [(u, v) for u, v in st.items()
                if v.get("phase") not in ("done",) and v.get("error")]
    if problems:
        print("")
        print("  Cases to review:")
        for u, v in problems[:10]:
            print(f"    {u[-12:]}  {v.get('phase')}  {v.get('error', '')[:70]}")
    print("")
    print("  The old versions are in Proton's trash, recoverable.")
    print("  After checking, regenerate inventory, map and albums:")
    print("    ./photo-backup.py DEST --details")
    print("    ./photo-fixdate.py DEST --reorganize")
    print("    ./album-backup.py DEST --album-dir ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
