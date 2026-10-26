#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
file-backup.py - Incremental backup of Proton Drive files to a local disk.

WHY
  `proton-drive filesystem download -f skip` skips files that already exist
  by NAME, so it never notices that a file changed. This script first scans
  Proton Drive and gets the size and sha1 of every remote file, so it can
  tell not only what is missing from the backup but also what changed since
  last time, and (with --verify) whether a local copy got silently corrupted
  while keeping the exact same size.

WHAT IT DOES
  1. scans the remote folder (--root, default /my-files) through the CLI:
     seconds even on tens of thousands of files, where the macOS Drive folder
     takes hours. The scan is saved in ~/drive-sync-logs; if it gets
     interrupted, run the same command again and it resumes.
  2. compares it with the files in the backup folder
  3. downloads what is missing and what has a different size
  4. with --verify, recomputes the local sha1 and re-downloads any mismatch

  Files are downloaded in batches grouped by destination folder, because the
  CLI downloads into one folder per call. Batches that fail are listed in
  ~/drive-sync-logs/files-not-downloaded.txt; just run again and it retries
  only what is still missing.

  Note: Proton reports these hashes as `sha1Verified: false`, i.e. declared by
  the uploading client, so --verify confirms that the backup matches what
  Proton *declares* to hold.

USAGE
  ./file-backup.py /path/to/backup/proton-files --verify      # monthly
  ./file-backup.py /path/to/backup/proton-files --dry-run
  ./file-backup.py /path/to/backup/work --root /my-files/Work

SAFETY
  Downloads only. Never modifies or deletes anything on Proton.
  Local files that no longer exist on Proton are reported, never removed.
"""

import argparse
import os
import subprocess
import sys
import time

import protonlib


def parse_args():
    p = argparse.ArgumentParser(
        description="Incremental backup of Proton Drive files.")
    p.add_argument("dest", help="backup folder, e.g. /path/to/backup/proton-files")
    p.add_argument("--root", default="/my-files",
                   help="remote folder to back up (default /my-files, the whole Drive)")
    p.add_argument("--verify", action="store_true",
                   help="recompute the sha1 of every local file (slow but safe)")
    p.add_argument("--reuse-scan", action="store_true",
                   help="reuse the last complete scan instead of scanning Proton again")
    p.add_argument("--workers", type=int, default=3,
                   help="parallel calls while scanning (default 3)")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI, help="path to the CLI binary")
    p.add_argument("--batch", type=int, default=20,
                   help="files per download call (default 20)")
    p.add_argument("--timeout", type=int, default=1800,
                   help="timeout per download call, in seconds")
    p.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    p.add_argument("--max-print", type=int, default=25)
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    return a


def get_scan(a):
    """Path of an up-to-date, complete scan of a.root."""
    tsv = protonlib.scan_path(a.root)
    if a.reuse_scan:
        if not os.path.exists(tsv) or os.path.exists(tsv + ".state"):
            sys.exit(f"ERROR: no complete scan of {a.root} to reuse. Run without --reuse-scan.")
        print(f"  Scan reused: {tsv}")
        return tsv
    tsv, errors = protonlib.scan_drive(a.cli, a.root, workers=a.workers)
    if errors:
        protonlib.scan_errors_exit(errors)
    return tsv


def load_remote(tsv):
    remote = {}
    for rel, size, mtime, sha1, flag in protonlib.read_scan(tsv):
        # the normalized key is for comparison; the ORIGINAL path is for the
        # CLI, which only accepts the exact form Proton stored (NFD on macOS)
        remote[protonlib.nfc(rel)] = (size, sha1, rel)
    return remote


def main():
    a = parse_args()
    print("")
    tsv = get_scan(a)
    os.makedirs(a.dest, exist_ok=True)

    remote = load_remote(tsv)
    print("")
    print(f"  PROTON : {a.root}  ({len(remote)} files)")
    print(f"  BACKUP : {a.dest}")
    print("")

    missing, different, ok_files, no_hash = [], [], 0, 0
    verified = 0

    for rel, (size, sha1, rel_orig) in remote.items():
        local = os.path.join(a.dest, rel)
        if not os.path.exists(local):
            alt = os.path.join(a.dest, protonlib.as_local(rel))
            if alt != local and os.path.exists(alt):
                local = alt
            else:
                missing.append((rel, size))
                continue
        try:
            lsize = os.path.getsize(local)
        except OSError:
            missing.append((rel, size))
            continue
        if lsize != size:
            different.append((rel, size, lsize))
            continue
        if a.verify:
            if sha1 == "-" or not sha1:
                no_hash += 1
            else:
                h = protonlib.sha1_of(local)
                verified += 1
                if verified % 100 == 0:
                    sys.stderr.write(f"\r  verifying sha1: {verified} files...")
                if h != sha1:
                    different.append((rel, size, lsize))
                    continue
        ok_files += 1
    if a.verify:
        sys.stderr.write("\r" + " " * 40 + "\r")

    # local files that no longer exist on the remote
    local_files = set()
    for dirpath, dirnames, filenames in os.walk(a.dest):
        for fn in filenames:
            if fn.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), a.dest)
            local_files.add(protonlib.nfc(rel))
    expected = set(remote) | {protonlib.nfc(protonlib.as_local(r)) for r in remote}
    local_only = sorted(local_files - expected)

    to_download = [(r, s) for r, s in missing] + [(r, s) for r, s, _ in different]
    bytes_to_download = sum(s for _, s in to_download)

    print("=" * 66)
    print(f"  Already in sync ............ {ok_files}")
    print(f"  Missing from backup ........ {len(missing)}  "
          f"({protonlib.human(sum(s for _, s in missing))})")
    print(f"  Different (re-download) .... {len(different)}")
    if a.verify:
        print(f"  Verified by content ........ {verified}")
        print(f"  No hash on remote .......... {no_hash}")
    print(f"  Only in backup (not on Proton) {len(local_only)}")
    print("=" * 66)

    if local_only:
        print("")
        print("  In the backup but no longer on Proton (NOT touched):")
        for r in local_only[:a.max_print]:
            print("     " + r)
        if len(local_only) > a.max_print:
            print(f"     ... and {len(local_only) - a.max_print} more")

    if not to_download:
        print("")
        print("  Backup in sync: nothing to download.")
        return 0

    print("")
    print(f"  To download: {len(to_download)} files ({protonlib.human(bytes_to_download)})")
    for r, s in to_download[:a.max_print]:
        print(f"     {protonlib.human(s):>10}  {r}")
    if len(to_download) > a.max_print:
        print(f"     ... and {len(to_download) - a.max_print} more")

    if a.dry_run:
        print("")
        print("  (--dry-run: nothing downloaded)")
        return 0

    # group by destination folder: the CLI downloads into a single folder
    orig = {k: v[2] for k, v in remote.items()}
    by_folder = {}
    for rel, _ in to_download:
        by_folder.setdefault(os.path.dirname(protonlib.as_local(rel)), []).append(rel)

    print("")
    done, failed = 0, []
    t0 = time.time()
    for sub, rels in by_folder.items():
        target = os.path.join(a.dest, sub) if sub else a.dest
        os.makedirs(target, exist_ok=True)
        for i in range(0, len(rels), a.batch):
            batch = rels[i:i + a.batch]
            remotes = [a.root.rstrip("/") + "/" + orig.get(r, r) for r in batch]
            cmd = ["filesystem", "download", "-f", "remove", "-d", "merge"] \
                + remotes + [target]
            try:
                r = protonlib.run_cli(a.cli, cmd, a.timeout)
                ok = (r.returncode == 0)
                err = (r.stderr or r.stdout or "").strip()[:200]
            except subprocess.TimeoutExpired:
                ok, err = False, f"timed out after {a.timeout}s"
            if ok:
                done += len(batch)
            else:
                for rel in batch:
                    failed.append((rel, err))
                sys.stderr.write(f"\n  batch failed in {sub or '/'}: {err}\n")
            el = int(time.time() - t0)
            sys.stderr.write(f"\r  {done}/{len(to_download)} files | {el}s      ")
            sys.stderr.flush()

    sys.stderr.write("\n")
    print("")
    print(f"  Downloaded : {done}")
    print(f"  Failed     : {len(failed)}")
    if failed:
        os.makedirs(protonlib.LOG_DIR, exist_ok=True)
        p = os.path.join(protonlib.LOG_DIR, "files-not-downloaded.txt")
        with open(p, "w", encoding="utf-8") as f:
            for rel, e in failed:
                f.write(f"{rel}\t{e}\n")
        print(f"  List       : {p}")
        print("  Rerun the script: it retries only what is still missing.")
        return 1
    print("")
    print("  Backup in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
