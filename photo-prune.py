#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-prune.py - Propagate to Proton the photo deletions decided locally.

WHY
  Culling a large photo library is much easier (and safer) by browsing the
  local backup in Finder than in a web interface: you look at the photos,
  delete the ones you don't want, and the macOS Trash is your safety net.
  This script then replays those deletions on Proton, moving the matching
  photos to Proton's trash.

TWO MODES
  --from-missing   compares the uid -> file map with the disk: whatever you
                   already deleted by hand from the backup is trashed on
                   Proton too. This is the usual workflow.

  --from-list FILE uses a file with lines "delete <path>" (paths relative to
                   the backup folder), e.g. produced by a duplicate review.
                   Deletes the local files, then trashes the matching uids.
                   Use --keep-local to keep the local copies.

  The uid of each file comes from .proton-photo-map.json, built by
  photo-backup.py on content (sha1). photo-backup.py keeps the last known
  path of photos deleted from the backup, so the order doesn't matter: it
  also lists them in photos-deleted-locally.txt and suggests this script.

SAFETY
  - dry run by default: nothing happens without --apply
  - refuses to proceed if an album would be left empty
  - Proton's trash has no expiry, so every deletion stays recoverable
  - resumable: progress is saved in prune-state.json in the backup folder

  After pruning, bring the backup in line (without --reuse-inventory):
      ./photo-backup.py

PROFILES
  The backup folder comes from the photo-backup.py profile of the same name
  ([photo-backup <name>] in ~/.config/proton-backup/config.ini, or --config):
  ./photo-prune.py ssd works on its dest, and with a single photo-backup
  profile no name is needed. An optional [photo-prune <name>] section can set
  timeout. A folder path works too.

USAGE
  ./photo-prune.py --from-missing --dry-run
  ./photo-prune.py --from-missing --apply
  ./photo-prune.py ssd --from-list duplicates.txt --apply
  ./photo-prune.py /path/to/backup/proton-photos --from-missing --apply
"""

import argparse
import json
import os
import sys
import time

import protonlib

PHOTO_MAP = ".proton-photo-map.json"
ALBUM_MAP = ".proton-albums.json"
STATE = "prune-state.json"


SCRIPT = "photo-prune"


def parse_args():
    p = argparse.ArgumentParser(
        description="Propagate local photo deletions to Proton.",
        epilog="Settings: ~/.config/proton-backup/config.ini (see config.example.ini). "
               "The backup folder comes from the photo-backup profile of the same name.")
    p.add_argument("target", nargs="?",
                   help="a photo-backup profile (optional when there's only one), "
                        "or the photo backup folder")
    p.add_argument("--config", default=protonlib.CONFIG_PATH,
                   help="configuration file (default ~/.config/proton-backup/config.ini)")
    p.add_argument("--from-list", default=None,
                   help="file with lines 'delete <path>'")
    p.add_argument("--from-missing", action="store_true",
                   help="trash on Proton whatever is no longer on disk")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI)
    p.add_argument("--keep-local", action="store_true",
                   help="don't delete local files, only trash on Proton")
    p.add_argument("--apply", action="store_true", help="actually do it")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="handle at most N photos")
    p.add_argument("--timeout", type=int,
                   help="timeout per CLI call, in seconds (default 120)")
    a = p.parse_args()
    a.config = os.path.expanduser(a.config)
    if not a.from_list and not a.from_missing:
        sys.exit("Choose --from-list FILE or --from-missing")
    return a


def resolve_settings(a):
    """The backup folder and the timeout, copied onto `a`."""
    cfg = protonlib.read_config(a.config)
    name = protonlib.choose_profile(cfg, SCRIPT, a.target, a.config)
    s = protonlib.load_settings(cfg, SCRIPT, name)
    if name is None:
        s["dest"] = a.target
        where = "command line"
    else:
        where = f"profile [photo-backup {name}] in {a.config}"
        if not s["dest"]:
            sys.exit(f"ERROR: [photo-backup {name}] in {a.config} has no dest. "
                     f"Run: ./photo-backup.py --setup {name}")
    if a.timeout is not None:
        s["timeout"] = str(a.timeout)
    protonlib.check_settings(s, where)
    a.dest = os.path.expanduser(s["dest"])
    a.timeout = s["timeout"]
    a.where = where
    if not os.path.isdir(a.dest):
        sys.exit(f"ERROR: {a.dest} doesn't exist ({where}). Is the disk plugged in?")
    return a



def main():
    a = parse_args()
    resolve_settings(a)
    print(f"  Backup : {a.dest}   ({a.where})")
    mp = os.path.join(a.dest, PHOTO_MAP)
    if not os.path.exists(mp):
        sys.exit(f"ERROR: {mp} is missing. Run photo-backup.py first.")
    with open(mp, encoding="utf-8") as f:
        photo_map = json.load(f)
    by_path = {}
    for u, p in photo_map.items():
        by_path.setdefault(p, []).append(u)

    albums = []
    ap = os.path.join(a.dest, ALBUM_MAP)
    if os.path.exists(ap):
        with open(ap, encoding="utf-8") as f:
            albums = json.load(f)
    albums_of = {}
    for al in albums:
        for u in al["photos"]:
            albums_of.setdefault(u, []).append(al["name"])

    # --- what to remove ------------------------------------------------------
    to_remove = []          # (uid, path)
    if a.from_list:
        f = a.from_list
        if not os.path.isabs(f):
            cand = os.path.join(a.dest, f)
            f = cand if os.path.exists(cand) else f
        if not os.path.exists(f):
            sys.exit(f"ERROR: {f} doesn't exist")
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line.startswith("delete "):
                    continue
                rel = line[len("delete "):].strip()
                full = os.path.join(a.dest, rel)
                uids = by_path.get(full)
                if not uids:
                    print(f"  WARNING: not in the map, skipping: {rel}")
                    continue
                for u in uids:
                    to_remove.append((u, full))
    else:
        for u, p in photo_map.items():
            if not os.path.exists(p):
                to_remove.append((u, p))

    if a.limit:
        to_remove = to_remove[:a.limit]

    # --- albums that would be left empty -------------------------------------
    removed_uids = {u for u, _ in to_remove}
    emptied = []
    for al in albums:
        remaining = [u for u in al["photos"] if u not in removed_uids]
        if al["photos"] and not remaining:
            emptied.append(al["name"])
    in_album = [(u, p) for u, p in to_remove if albums_of.get(u)]

    print("")
    print(f"  Backup   : {a.dest}")
    print(f"  To trash on Proton    : {len(to_remove)}")
    print(f"  of which in an album  : {len(in_album)}")
    if not a.keep_local and a.from_list:
        print(f"  Local files to delete : "
              f"{len({p for _, p in to_remove if os.path.exists(p)})}")
    if emptied:
        print("")
        print(f"  WARNING: {len(emptied)} albums would be left EMPTY:")
        for s in emptied[:10]:
            print(f"     {s}")

    if not to_remove:
        print("\n  Nothing to do.")
        return 0

    if not a.apply or a.dry_run:
        print("")
        for u, p in to_remove[:15]:
            al = ", ".join(albums_of.get(u, [])) or "-"
            print(f"  {p.replace(a.dest + '/', '')}   [{al}]")
        if len(to_remove) > 15:
            print(f"  ... and {len(to_remove) - 15} more")
        print("")
        print("  (nothing changed: add --apply to execute)")
        return 0

    if emptied:
        print("")
        print("  Refusing to proceed: some albums would be left empty.")
        print("  Review the list, or delete those albums by hand first.")
        return 1

    # --- execution -----------------------------------------------------------
    state_path = os.path.join(a.dest, STATE)
    st = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f)

    def save_state():
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)

    trashed = local_removed = errors = 0
    t0 = time.time()
    for i, (u, p) in enumerate(to_remove, 1):
        if st.get(u) == "done":
            continue
        r = protonlib.run_cli(a.cli, ["filesystem", "trash", f"/photos/{u}"], a.timeout)
        if r.returncode != 0:
            st[u] = "error: " + (r.stderr or r.stdout or "")[:120]
            errors += 1
        else:
            trashed += 1
            if not a.keep_local and os.path.exists(p):
                try:
                    os.remove(p)
                    local_removed += 1
                except OSError as e:
                    st[u] = f"trashed but local file not removed: {e}"
            st[u] = st.get(u) if str(st.get(u, "")).startswith("trashed") else "done"
        if i % 20 == 0:
            save_state()
            sys.stderr.write(f"\r  {i}/{len(to_remove)} | {int(time.time()-t0)}s   ")
            sys.stderr.flush()
    save_state()
    sys.stderr.write("\n")

    print("")
    print(f"  Trashed on Proton   : {trashed}")
    print(f"  Local files removed : {local_removed}")
    print(f"  Errors              : {errors}")
    print("")
    print("  The photos are in Proton's trash, recoverable with no expiry.")
    print("  After checking, regenerate inventory, map and albums:")
    print("    ./photo-backup.py" + (f" {a.dest}" if a.where == "command line" else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
