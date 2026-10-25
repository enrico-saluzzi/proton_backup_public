#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
album-backup.py - Back up the album structure of Proton Photos.

WHY
  `photo timeline` is flat: it knows nothing about albums. Without this
  script the backup holds every photo but loses how you organized them.

WHAT IT DOES
  1. reads `album list` and `album photos` and saves the full album -> photos
     map as .proton-albums.json in the backup folder
  2. rebuilds each album as a folder of HARD LINKS to the photos already
     downloaded by photo-backup.py

  Hard links point to the same data on disk: albums can be browsed as real
  folders but take no extra space, even when a photo belongs to several
  albums. The same backup therefore offers two views: the chronological one
  (year folders) and the album one. Album and photo folders must be on the
  same volume (falls back to symlinks otherwise).

  Photos are matched to local files through .proton-photo-map.json, built by
  photo-fixdate.py on content (sha1). Without it, matching falls back to file
  names, which is ambiguous with duplicate names.

  Photos in an album with no local file are reported as "not linked": either
  they're not downloaded yet (run photo-backup.py, then rerun with --reuse),
  or the map is stale (run photo-fixdate.py first). photo-unlinked.py tells
  exactly which ones and why.

  After pruning photos, DON'T use --reuse: the saved album map still holds
  uids that are now in the trash, and they'd show up as "not linked".

  Two different photos of the same album may share a file name (e.g. two
  IMG_0001.jpg from different years): the second one gets a " (1)" suffix.
  A file already in the album folder with the same content counts as already
  present, so reruns never create duplicates.

  Album folders only grow: links to photos later removed from an album or
  trashed are never deleted. To rebuild them cleanly, delete the album
  folder and rerun (hard links cost nothing to recreate).

USAGE
  ./album-backup.py /path/to/backup/proton-photos
  ./album-backup.py /path/to/backup/proton-photos --album-dir /path/to/backup/proton-albums
  ./album-backup.py /path/to/backup/proton-photos --reuse      # reuse the album map
  ./album-backup.py /path/to/backup/proton-photos --no-link    # save the map only

SAFETY
  Read-only on Proton. Locally it only creates links: it doesn't copy or
  delete anything.
"""

import argparse
import json
import os
import sys
import time

import protonlib

ALBUM_MAP = ".proton-albums.json"
INVENTORY = ".proton-photo-inventory.json"
PHOTO_MAP = ".proton-photo-map.json"


def parse_args():
    p = argparse.ArgumentParser(description="Back up the album structure.")
    p.add_argument("dest", help="photo backup folder")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI)
    p.add_argument("--reuse", action="store_true",
                   help="reuse the saved album map instead of reading it from Proton")
    p.add_argument("--no-link", action="store_true",
                   help="save the map only, don't create album folders")
    p.add_argument("--photo-dir", default=None,
                   help="where the photos are (default: the backup folder itself)")
    p.add_argument("--album-dir", default="album",
                   help="folder for the albums, relative to the backup folder or "
                        "absolute (default: album)")
    p.add_argument("--timeout", type=int, default=300)
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    if not a.photo_dir:
        a.photo_dir = a.dest
    a.photo_dir = os.path.expanduser(a.photo_dir)
    return a


def cli(a, cmd):
    return protonlib.run_cli(a.cli, cmd, a.timeout)


def safe_name(name):
    """A name usable as a folder name on macOS."""
    for ch in (":", "/", "\\", "\0"):
        name = name.replace(ch, "_")
    return name.strip() or "unnamed"


def same_photo(a_path, b_path):
    """Same file (hard link), or a different file with identical content."""
    if os.path.samefile(a_path, b_path):
        return True
    h = protonlib.sha1_of(a_path)
    return h is not None and h == protonlib.sha1_of(b_path)


def read_album_map(a):
    map_path = os.path.join(a.dest, ALBUM_MAP)
    if a.reuse and os.path.exists(map_path):
        with open(map_path, encoding="utf-8") as f:
            m = json.load(f)
        print(f"  Album map reused: {len(m)} albums")
        return m

    print("  Reading albums from Proton...")
    r = cli(a, ["album", "list", "-j"])
    if r.returncode != 0:
        sys.exit(f"ERROR from 'album list': {(r.stderr or r.stdout)[:300]}")
    try:
        albums = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: 'album list' didn't return valid JSON: {e}")

    print(f"  {len(albums)} albums found. Reading their content...")
    album_map = []
    t0 = time.time()
    for i, alb in enumerate(albums, 1):
        uid = alb.get("uid")
        name = ((alb.get("name") or {}).get("value")
                or f"album-{uid[-8:] if uid else i}")
        expected = (alb.get("album") or {}).get("photoCount", 0)
        cover = (alb.get("album") or {}).get("coverPhotoNodeUid")

        sys.stderr.write(f"\r  {i}/{len(albums)}  {name[:44]:<44}")
        sys.stderr.flush()

        try:
            r = cli(a, ["album", "photos", "-j", f"/albums/{uid}"])
            photos = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
            err = None if r.returncode == 0 else (r.stderr or "")[:150]
        except Exception as e:
            photos, err = [], str(e)[:150]

        uids = [f.get("nodeUid") for f in photos if f.get("nodeUid")]
        album_map.append({
            "uid": uid, "name": name, "photoCount": expected,
            "coverPhotoNodeUid": cover, "photos": uids,
            "creationTime": alb.get("creationTime"),
            "error": err,
        })
        if err or (expected and len(uids) != expected):
            sys.stderr.write(f"\n    WARNING: {name}: expected {expected}, "
                             f"read {len(uids)}{' - ' + err if err else ''}\n")
    sys.stderr.write("\r" + " " * 70 + "\r")
    print(f"  Done in {int(time.time()-t0)}s")

    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(album_map, f, ensure_ascii=False, indent=1)
    print(f"  Map saved: {map_path}")
    return album_map


def local_index(a):
    """Photo uid -> path of the downloaded file.

    If photo-fixdate.py already produced the sha1-based map, that one is used:
    it's exact even with duplicate names, where matching by name can't decide
    which file belongs to which uid.
    """
    mp = os.path.join(a.dest, PHOTO_MAP)
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        alive = {u: p for u, p in m.items() if os.path.exists(p)}
        print(f"  sha1 map found: {len(alive)} photos matched")
        if len(alive) < len(m):
            print(f"  ({len(m) - len(alive)} paths no longer valid: "
                  f"rerun photo-fixdate.py if you moved files)")
        return alive, {"ambiguous": 0, "inventory": len(m)}

    print("  No sha1 map: matching by name (less precise).")
    print("  Run photo-fixdate.py first for an exact result.")
    inv_path = os.path.join(a.dest, INVENTORY)
    if not os.path.exists(inv_path):
        return {}, {}
    with open(inv_path, encoding="utf-8") as f:
        inv = json.load(f)

    by_name = {}
    for x in inv:
        uid = x.get("uid") or x.get("nodeUid")
        name = (x.get("name") or {}).get("value")
        if uid and name:
            by_name.setdefault(name, []).append(uid)

    # files on disk, indexed by name
    files_by_name = {}
    album_dir = os.path.join(a.dest, a.album_dir)
    for dirpath, dirnames, filenames in os.walk(a.photo_dir):
        if dirpath.startswith(album_dir):
            continue
        for fn in filenames:
            if fn.startswith("."):
                continue
            files_by_name.setdefault(fn, []).append(os.path.join(dirpath, fn))

    uid2file = {}
    ambiguous = 0
    for name, uids in by_name.items():
        paths = files_by_name.get(name, [])
        if len(uids) == 1 and len(paths) == 1:
            uid2file[uids[0]] = paths[0]
        elif paths:
            # duplicate names: the CLI added a suffix, they can't be matched
            # with certainty without hashing
            ambiguous += len(uids)
    return uid2file, {"ambiguous": ambiguous, "inventory": len(inv)}


def main():
    a = parse_args()
    os.makedirs(a.dest, exist_ok=True)

    print("")
    print(f"  Backup   : {a.dest}")
    print("")

    album_map = read_album_map(a)
    tot_photos = sum(len(x["photos"]) for x in album_map)
    print("")
    print(f"  Albums   : {len(album_map)}")
    print(f"  Photos in albums (with repeats): {tot_photos}")
    print(f"  Distinct photos: {len(set(u for x in album_map for u in x['photos']))}")

    problems = [x for x in album_map if x.get("error")
                or (x["photoCount"] and len(x["photos"]) != x["photoCount"])]
    if problems:
        print("")
        print(f"  WARNING: {len(problems)} albums with a mismatching count:")
        for x in problems[:10]:
            print(f"    {x['name']}: expected {x['photoCount']}, read {len(x['photos'])}")

    if a.no_link:
        print("")
        print("  (--no-link: map saved, no folder created)")
        return 0

    uid2file, stat = local_index(a)
    if not uid2file:
        print("")
        print("  No local photo could be matched: the inventory is missing or")
        print("  the photos aren't downloaded yet. The map is saved anyway.")
        return 0

    print("")
    print(f"  Local photos matched: {len(uid2file)}")
    if stat.get("ambiguous"):
        print(f"  Not matchable because of duplicate names: {stat['ambiguous']}")

    base = os.path.join(a.dest, a.album_dir)
    os.makedirs(base, exist_ok=True)

    created = missing = existing = 0
    for x in album_map:
        folder = os.path.join(base, safe_name(x["name"]))
        os.makedirs(folder, exist_ok=True)
        for u in x["photos"]:
            src = uid2file.get(u)
            if not src:
                missing += 1
                continue
            # two different photos of the same album can share a file name
            # (e.g. IMG_0001.jpg from different years): a file already in the
            # album folder counts as "this photo" only if it has the same content
            dst = os.path.join(folder, os.path.basename(src))
            stem, ext = os.path.splitext(dst)
            n = 1
            while os.path.exists(dst) and not same_photo(src, dst):
                dst = f"{stem} ({n}){ext}"
                n += 1
            if os.path.exists(dst):
                existing += 1
                continue
            try:
                os.link(src, dst)
                created += 1
            except OSError:
                try:
                    os.symlink(src, dst)
                    created += 1
                except OSError:
                    missing += 1

    print("")
    print(f"  Links created      : {created}")
    print(f"  Already present    : {existing}")
    print(f"  Not linked         : {missing}")
    print("")
    print(f"  Albums in: {base}")
    if missing:
        print("  Some album photos have no local file: run photo-backup.py and")
        print("  photo-fixdate.py, then rerun with --reuse. photo-unlinked.py")
        print("  lists exactly which photos and why.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
