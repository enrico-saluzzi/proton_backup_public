#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
proton-orphans.py - What is on Proton Drive that no longer exists locally?

WHY
  After pruning a large local archive, the folders you deleted locally often
  stay on the cloud forever, because nobody remembers what was removed. This
  script answers "what should I delete on Proton" without having to remember
  what you deleted locally.

WHAT IT DOES
  Compares a Proton scan (the TSV from proton-scan.py) with a local folder
  and finds the FOLDERS that exist only on the remote. Each orphan folder is
  reported at the highest possible level: if a whole branch is missing, you
  see the branch, not its hundreds of subfolders. Single files missing
  locally inside folders that exist on both sides are listed separately.

  Full lists are written to --out-dir (default ~/drive-sync-logs).

USAGE
  ./proton-orphans.py ~/Dropbox my-files.tsv
  ./proton-orphans.py ~/Dropbox my-files.tsv --min-files 10
  ./proton-orphans.py ~/Dropbox my-files.tsv --prefix Work

SAFETY
  DELETES NOTHING. It produces a list to act on by hand, because deleting
  based on a partial scan or a wrong comparison is the fastest way to lose
  data.
"""

import argparse
import os
import sys
import time

import protonlib

JUNK_DIRS = {".Trashes", ".Trash", ".TemporaryItems", ".Spotlight-V100",
             ".fseventsd", ".DocumentRevisions-V100", "#recycle",
             ".dropbox.cache"}


def parse_args():
    p = argparse.ArgumentParser(
        description="Folders that exist on Proton but no longer locally.")
    p.add_argument("local", help="local reference folder, e.g. ~/Dropbox")
    p.add_argument("scan", help="TSV produced by proton-scan.py")
    p.add_argument("--prefix", default="",
                   help="prefix to strip from the paths in the scan")
    p.add_argument("--min-files", type=int, default=1,
                   help="ignore folders with fewer files than this (default 1)")
    p.add_argument("--out-dir", default=os.path.expanduser("~/drive-sync-logs"))
    p.add_argument("--max-print", type=int, default=40)
    a = p.parse_args()
    a.out_dir = os.path.expanduser(a.out_dir)
    if os.sep not in a.scan:
        cand = os.path.join(a.out_dir, a.scan)
        if os.path.exists(cand):
            a.scan = cand
    return a


def main():
    a = parse_args()
    root = os.path.expanduser(a.local)
    if not os.path.isdir(root):
        sys.exit(f"ERROR: {root} doesn't exist")
    if not os.path.exists(a.scan):
        sys.exit(f"ERROR: {a.scan} doesn't exist")

    # --- folders and files on Proton -----------------------------------------
    remote_files = {}
    remote_dirs = set()
    with open(a.scan, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            rel, size = parts[0], parts[1]
            if a.prefix and rel.startswith(a.prefix):
                rel = rel[len(a.prefix):].lstrip("/")
            rel = protonlib.nfc(rel)
            try:
                remote_files[rel] = int(size or 0)
            except ValueError:
                remote_files[rel] = 0
            d = os.path.dirname(rel)
            while d:
                remote_dirs.add(d)
                d = os.path.dirname(d)

    # --- folders present locally ---------------------------------------------
    local_dirs = set()
    local_files = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in JUNK_DIRS]
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            local_dirs.add(protonlib.nfc(rel_dir))
        for fn in filenames:
            rel = os.path.join(rel_dir, fn) if rel_dir != "." else fn
            local_files.add(protonlib.nfc(rel))
        sys.stderr.write(f"\r  local: {len(local_dirs)} folders...")
    sys.stderr.write("\r" + " " * 40 + "\r")

    # --- orphan folders, reported at the highest level ----------------------
    orphan_dirs = sorted(d for d in remote_dirs if d not in local_dirs)
    top = []
    for d in orphan_dirs:
        parent = os.path.dirname(d)
        covered = False
        while parent:
            if parent in orphan_dirs:
                covered = True
                break
            parent = os.path.dirname(parent)
        if not covered:
            top.append(d)

    stats = []
    for d in top:
        pref = d + "/"
        n = 0
        b = 0
        for rel, size in remote_files.items():
            if rel.startswith(pref):
                n += 1
                b += size
        if n >= a.min_files:
            stats.append((n, b, d))
    stats.sort(reverse=True)

    # --- orphan files in folders that exist on both sides --------------------
    lone_files = []
    for rel, size in remote_files.items():
        if rel in local_files:
            continue
        d = os.path.dirname(rel)
        if d and d not in local_dirs:
            continue          # already covered by an orphan folder
        lone_files.append((size, rel))
    lone_files.sort(reverse=True)

    # --- report --------------------------------------------------------------
    print("")
    print(f"  LOCAL  : {root}")
    print(f"  PROTON : {a.scan}  ({len(remote_files)} files, {len(remote_dirs)} folders)")
    print("")
    print("=" * 78)
    print(f"  Folders ONLY on Proton ........ {len(stats)}")
    print(f"  Loose files ONLY on Proton .... {len(lone_files)}")
    print("=" * 78)

    if stats:
        tot_n = sum(s[0] for s in stats)
        tot_b = sum(s[1] for s in stats)
        print("")
        print("FOLDERS TO CONSIDER FOR DELETION ON PROTON")
        print("(they exist in the cloud but no longer locally)")
        print("")
        print(f"{'FILES':>8} {'SIZE':>10}   PATH")
        print("-" * 78)
        for n, b, d in stats[:a.max_print]:
            print(f"{n:>8} {protonlib.human(b):>10}   {d}")
        if len(stats) > a.max_print:
            print(f"   ... and {len(stats) - a.max_print} more")
        print("-" * 78)
        print(f"{tot_n:>8} {protonlib.human(tot_b):>10}   TOTAL")

    if lone_files:
        print("")
        print("SINGLE FILES only on Proton")
        print("(the folder exists on both sides: review them one by one)")
        print("")
        for size, rel in lone_files[:a.max_print]:
            print(f"{protonlib.human(size):>10}   {rel}")
        if len(lone_files) > a.max_print:
            print(f"   ... and {len(lone_files) - a.max_print} more")

    os.makedirs(a.out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    p1 = os.path.join(a.out_dir, f"proton-orphan-folders-{stamp}.txt")
    with open(p1, "w", encoding="utf-8") as f:
        for n, b, d in stats:
            f.write(f"{d}\n")
    p2 = os.path.join(a.out_dir, f"proton-orphan-files-{stamp}.txt")
    with open(p2, "w", encoding="utf-8") as f:
        for size, rel in lone_files:
            f.write(f"{rel}\n")

    print("")
    print("Full lists:")
    print(f"   {p1}")
    print(f"   {p2}")
    print("")
    print("WARNING: if the Proton scan is incomplete, so is this list. And it is a")
    print("list of CANDIDATES: before deleting, check that the folder is really")
    print("covered elsewhere (GitHub, an archive, or because you deleted it on")
    print("purpose).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
