#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
drive-compare.py - Compare a local folder with a Proton Drive inventory.

WHY
  When migrating to Proton Drive (e.g. from Dropbox), you need to know that
  everything actually arrived before you delete the old copy. This script
  takes the TSV produced by proton-scan.py and compares it with a local
  folder. Proton exposes the sha1 of each original file, so with --sha1 the
  comparison goes down to the CONTENT, without downloading anything from
  Proton: computing the sha1 locally is enough.

WHAT IT REPORTS
  - files only in the local folder (missing on Proton)
  - files only on Proton (missing locally)
  - files with a different size, or (with --sha1) different content
  - files that exist on both sides but are "online-only" locally (cloud
    placeholders that were never downloaded): their content can't be
    verified without downloading them, so they're listed separately

  Most differences in a real migration are structural noise: the content
  of macOS packages (.pages, .numbers, ...), Icon files, Dropbox service
  files, Office lock files. They are grouped and counted separately; only
  the TO-REVIEW lists need a human decision.

  Full lists are written to --out-dir (default ~/drive-sync-logs) as
  <list>-<label>-<timestamp>.txt.

USAGE
  ./drive-compare.py ~/Dropbox proton.tsv
  ./drive-compare.py ~/Dropbox proton.tsv --sha1          # compare content
  ./drive-compare.py ~/Dropbox my-files.tsv --sha1 --label check-$(date +%Y%m) \\
      --exclude-dirs "Camera Uploads,Photos"

  A bare TSV name is looked up in --out-dir, where proton-scan.py writes it.
  --exclude-dirs takes TOP-LEVEL folder names, comma-separated, exact match.

SAFETY
  Read-only. Doesn't copy, modify or delete anything. Note that
  --include-online FORCES the download of online-only files to read them.
"""

import argparse
import os
import re
import sys
import time

import protonlib

TOLERANCE = 2                # seconds of tolerance on modification times
SF_DATALESS = 0x40000000     # macOS flag on "online-only" files not materialized
SHA1_EMPTY = "da39a3ee5e6b4b0d3255bfef95601890afd80709"

JUNK = (".DS_Store", "desktop.ini", ".localized")
JUNK_DIRS = {".Trashes", ".Trash", ".TemporaryItems", ".Spotlight-V100",
             ".fseventsd", ".DocumentRevisions-V100", "#recycle",
             ".dropbox.cache"}

TO_REVIEW = "TO REVIEW"


def parse_args():
    p = argparse.ArgumentParser(description="Compare a local folder with a Proton scan.")
    p.add_argument("local", help="local folder, e.g. ~/Dropbox")
    p.add_argument("scan", help="TSV produced by proton-scan.py")
    p.add_argument("--prefix", default="",
                   help="prefix to strip from the paths in the scan")
    p.add_argument("--sha1", action="store_true",
                   help="compare content (computes sha1 locally: slower)")
    p.add_argument("--include-online", action="store_true",
                   help="also check online-only files: FORCES THEIR DOWNLOAD")
    p.add_argument("--exclude-dirs", default="",
                   help="top-level local folders to skip, comma-separated (e.g. Code,Work)")
    p.add_argument("--out-dir", default=os.path.expanduser("~/drive-sync-logs"))
    p.add_argument("--label", default=None,
                   help="label used in output file names "
                        "(default: name of the local folder)")
    p.add_argument("--max-print", type=int, default=30)
    a = p.parse_args()
    a.out_dir = os.path.expanduser(a.out_dir)
    if not a.label:
        a.label = os.path.basename(os.path.expanduser(a.local).rstrip(os.sep)) or "root"
    # no separators or spaces in file names
    a.label = re.sub(r"[^A-Za-z0-9._-]+", "_", a.label).strip("_") or "root"
    if os.sep not in a.scan:
        candidate = os.path.join(a.out_dir, a.scan)
        if os.path.exists(candidate):
            a.scan = candidate
    a.scan = os.path.expanduser(a.scan)
    return a


# Classification of differences: most are structural noise (content of macOS
# packages, service files) and must be kept apart from what actually needs a
# decision.
PKG_RE = re.compile(r'\.(lh3d|pages|numbers|key|band|rtfd|photoslibrary)/')
ICON_RE = re.compile(r'(^|/)Icon\r?$|(^|/)Icon\.jpg$')
DBX_RE = re.compile(r'^\.dropbox|\.dbx-(backup|passwords)$|(^|/)\.dropbox\.')
LOCK_RE = re.compile(r'(^|/)~\$')
PKGF_RE = re.compile(r'\.pkgf$')


def classify_local(rel):
    if PKG_RE.search(rel):
        return "content of macOS packages (.pages/.numbers/.lh3d)"
    if ICON_RE.search(rel):
        return "macOS Icon files"
    if DBX_RE.search(rel):
        return "Dropbox service files"
    if LOCK_RE.search(rel):
        return "Office lock files (~$)"
    return TO_REVIEW


def classify_remote(rel):
    if PKGF_RE.search(rel):
        return "macOS packages bundled by Proton (.pkgf)"
    if LOCK_RE.search(rel):
        return "Office lock files (~$)"
    if ICON_RE.search(rel):
        return "macOS Icon files"
    return TO_REVIEW


def group(items, fn):
    out = {}
    for it in items:
        out.setdefault(fn(it), []).append(it)
    return out


def scan_local(root, exclude):
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in JUNK_DIRS and os.path.join(
                           os.path.relpath(dirpath, root), d).lstrip("./") not in exclude]
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        if rel_dir.split(os.sep)[0] in exclude:
            continue
        for fn in filenames:
            if fn in JUNK or fn.startswith("._") or fn.startswith("~$"):
                continue
            rel = os.path.join(rel_dir, fn) if rel_dir else fn
            full = os.path.join(dirpath, fn)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            dataless = bool(getattr(st, "st_flags", 0) & SF_DATALESS)
            if not dataless and st.st_size == 0 and getattr(st, "st_blocks", 1) == 0:
                dataless = None   # suspicious: 0 bytes and 0 blocks, confirm with remote
            files[protonlib.nfc(rel)] = (st.st_size, int(st.st_mtime), full, dataless)
        sys.stderr.write(f"\r  local: {len(files)} files...")
    sys.stderr.write("\r" + " " * 40 + "\r")
    return files


def load_scan(path, prefix):
    remote = {}
    problems = []
    with open(path, encoding="utf-8", newline="\n") as f:
        for line in f:
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) < 5:
                continue
            rel, size, mtime, sha1, flag = parts[0], parts[1], parts[2], parts[3], parts[4]
            if prefix and rel.startswith(prefix):
                rel = rel[len(prefix):].lstrip("/")
            if flag != "-":
                problems.append((rel, flag))
            remote[protonlib.nfc(rel)] = (int(size or 0), int(mtime or 0), sha1)
    return remote, problems


def main():
    a = parse_args()
    local_root = os.path.expanduser(a.local)
    if not os.path.isdir(local_root):
        sys.exit(f"ERROR: {local_root} doesn't exist")
    exclude = set(x.strip() for x in a.exclude_dirs.split(",") if x.strip())

    print("")
    print(f"  LOCAL  : {local_root}" + (f"   (excluded: {', '.join(sorted(exclude))})" if exclude else ""))
    print(f"  PROTON : {a.scan}")
    print("")
    print("Reading...")

    remote, problems = load_scan(a.scan, a.prefix)
    local = scan_local(local_root, exclude)

    lk, rk = set(local), set(remote)
    only_local = sorted(lk - rk)
    only_remote = sorted(rk - lk)
    common = lk & rk

    same, size_diff, date_diff, content_diff, no_hash, online = [], [], [], [], [], []
    checked = 0
    t0 = time.time()

    for rel in sorted(common):
        ls, lm, lfull, ldataless = local[rel]
        rs, rm, rsha = remote[rel]

        # the file isn't physically present locally: no comparison is possible
        # without downloading it. "0 bytes locally, content on the remote" is
        # a placeholder, not a divergence.
        is_placeholder = (ldataless is True) or (
            ldataless is None and ls == 0 and rs > 0 and rsha != SHA1_EMPTY)
        if is_placeholder and not a.include_online:
            online.append(rel)
            continue

        if ls != rs:
            size_diff.append((rel, ls, rs))
            continue
        if a.sha1:
            if rsha == "-":
                no_hash.append(rel)
            else:
                h = protonlib.sha1_of(lfull)
                checked += 1
                if checked % 50 == 0:
                    sys.stderr.write(f"\r  sha1: {checked} files...")
                if h is None:
                    no_hash.append(rel)
                elif h != rsha:
                    content_diff.append(rel)
                    continue
                else:
                    same.append(rel)
                    continue
        if abs(lm - rm) > TOLERANCE:
            date_diff.append(rel)
        else:
            same.append(rel)
    if a.sha1:
        sys.stderr.write("\r" + " " * 40 + "\r")

    bytes_online = sum(remote[r][0] for r in online)
    bytes_local = sum(local[r][0] for r in only_local)
    bytes_remote = sum(remote[r][0] for r in only_remote)

    print("")
    print("=" * 66)
    print(f"  Local files ................... {len(local)}")
    print(f"  Files on Proton ............... {len(remote)}")
    print("-" * 66)
    if a.sha1:
        print(f"  Identical (content verified) .. {len(same)}")
    else:
        print(f"  Identical (name, size) ........ {len(same)}")
    n_rev_loc = len([x for x in only_local if classify_local(x) == TO_REVIEW])
    n_rev_rem = len([x for x in only_remote if classify_remote(x) == TO_REVIEW])
    print(f"  Local only .................. {len(only_local)}  ({protonlib.human(bytes_local)})"
          f"   to review: {n_rev_loc}")
    print(f"  Proton only ................. {len(only_remote)}  ({protonlib.human(bytes_remote)})"
          f"   to review: {n_rev_rem}")
    print(f"  Different size .............. {len(size_diff)}")
    if online:
        print(f"  Online-only, not verified ... {len(online)}  ({protonlib.human(bytes_online)})")
    if a.sha1:
        print(f"  DIFFERENT CONTENT ........... {len(content_diff)}")
        print(f"  Hash not available .......... {len(no_hash)}")
    else:
        print(f"  Different date .............. {len(date_diff)}")
    print("=" * 66)

    def dump(title, items, fmt=lambda x: x):
        if not items:
            return
        print("")
        print(title)
        for it in items[:a.max_print]:
            print("   " + fmt(it))
        if len(items) > a.max_print:
            print(f"   ... and {len(items) - a.max_print} more")

    if online:
        print("")
        print(f"  {len(online)} files exist on both sides but are NOT")
        print("  downloaded locally: their content can't be verified without")
        print("  downloading them. To really verify them, make the folder")
        print("  available offline in the sync client and rerun, or use")
        print("  --include-online (which forces their download).")

    gl = group(only_local, classify_local)
    gr = group(only_remote, classify_remote)

    def dump_grouped(title, groups, total):
        if not total:
            return
        print("")
        print(title)
        for k in sorted(groups, key=lambda x: (x != TO_REVIEW, -len(groups[x]))):
            n = len(groups[k])
            if k == TO_REVIEW:
                continue
            print(f"   {n:>5}  {k}")
        rev = groups.get(TO_REVIEW, [])
        print(f"   {len(rev):>5}  TO REVIEW  <-- the only ones that need a decision")
        if rev:
            print("")
            for it in rev[:a.max_print]:
                print("      " + it)
            if len(rev) > a.max_print:
                print(f"      ... and {len(rev) - a.max_print} more")

    dump_grouped("LOCAL ONLY (missing on Proton):", gl, len(only_local))
    dump_grouped("PROTON ONLY (missing locally):", gr, len(only_remote))
    dump("DIFFERENT SIZE:", size_diff,
         lambda t: f"{t[0]}  (local {t[1]} / Proton {t[2]})")
    if a.sha1:
        dump("DIFFERENT CONTENT with the same size:", content_diff)
    if problems:
        dump("PROBLEMATIC NODES on Proton:", problems, lambda t: f"{t[0]}  [{t[1]}]")

    os.makedirs(a.out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    written = []
    for name, items in (("TO-REVIEW-local", gl.get(TO_REVIEW, [])),
                        ("TO-REVIEW-proton", gr.get(TO_REVIEW, [])),
                        ("local-only", only_local),
                        ("proton-only", only_remote),
                        ("size-differs", [t[0] for t in size_diff]),
                        ("content-differs", content_diff),
                        ("hash-missing", no_hash),
                        ("online-only-unverified", online)):
        if not items:
            continue
        p = os.path.join(a.out_dir, f"{name}-{a.label}-{stamp}.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(items) + "\n")
        written.append(p)

    if written:
        print("")
        print("Full lists:")
        for p in written:
            print("   " + p)

    if a.sha1 and checked:
        print("")
        print(f"  {checked} files verified by content in {int(time.time() - t0)}s")

    print("")
    if not only_local and not only_remote and not size_diff and not content_diff:
        if online:
            print("  No file is missing on either side.")
            print(f"  {len(online)} files remain unverifiable because they're online-only.")
            return 0
        print("  The two copies match.")
        return 0
    print("  The two copies do NOT match: see the lists above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
