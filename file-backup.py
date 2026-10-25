#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
file-backup.py - Incremental backup of Proton Drive files to a local disk.

WHY
  `proton-drive filesystem download -f skip` skips files that already exist
  by NAME, so it never notices that a file changed. This script uses the TSV
  produced by proton-scan.py as the reference instead: it knows the size and
  sha1 of every remote file, so it can tell not only what is missing from the
  backup but also what changed since last time, and (with --verify) whether a
  local copy got silently corrupted while keeping the exact same size.

WHAT IT DOES
  1. reads the TSV (or regenerates it first with --rescan)
  2. compares it with the files in the backup folder
  3. downloads what is missing and what has a different size
  4. with --verify, recomputes the local sha1 and re-downloads any mismatch

  Files are downloaded in batches grouped by destination folder, because the
  CLI downloads into one folder per invocation. Batches that fail are listed
  in ~/drive-sync-logs/files-not-downloaded.txt; just rerun the script and it
  retries only what is still missing.

  Note: sha1 values in the inventory are those declared by the uploading
  client (Proton reports `sha1Verified: false`), so --verify confirms that the
  backup matches what Proton *declares* to hold.

USAGE
  ./file-backup.py /path/to/backup/proton-files
  ./file-backup.py /path/to/backup/proton-files --rescan --verify   # monthly
  ./file-backup.py /path/to/backup/proton-files --dry-run

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

DEFAULT_TSV = os.path.expanduser("~/drive-sync-logs/my-files.tsv")


def parse_args():
    p = argparse.ArgumentParser(
        description="Incremental backup of Proton Drive files.")
    p.add_argument("dest", help="backup folder, e.g. /path/to/backup/proton-files")
    p.add_argument("--tsv", default=DEFAULT_TSV, help=f"reference TSV (default {DEFAULT_TSV})")
    p.add_argument("--root", default="/my-files", help="remote root (default /my-files)")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI, help="path to the CLI binary")
    p.add_argument("--rescan", action="store_true",
                   help="regenerate the TSV with proton-scan.py before comparing")
    p.add_argument("--scanner", default=None,
                   help="path to proton-scan.py (default: next to this script)")
    p.add_argument("--verify", action="store_true",
                   help="recompute the sha1 of every local file (slow but safe)")
    p.add_argument("--batch", type=int, default=20,
                   help="files per CLI invocation (default 20)")
    p.add_argument("--timeout", type=int, default=1800, help="timeout per batch, in seconds")
    p.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    p.add_argument("--max-print", type=int, default=25)
    a = p.parse_args()
    a.dest = os.path.expanduser(a.dest)
    a.tsv = os.path.expanduser(a.tsv)

    # proton-scan.py lives next to this script: looking it up on PATH fails
    # when these tools aren't installed among the system commands
    if a.scanner:
        a.scanner = os.path.expanduser(a.scanner)
    else:
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "proton-scan.py")
        a.scanner = here if os.path.exists(here) else "proton-scan.py"
    return a


def load_tsv(path):
    remote = {}
    with open(path, encoding="utf-8", newline="\n") as f:
        for line in f:
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) < 5:
                continue
            rel, size, mtime, sha1 = parts[0], parts[1], parts[2], parts[3]
            if not rel.strip():
                continue
            try:
                size = int(size or 0)
            except ValueError:
                size = 0
            # the normalized key is for comparison; the ORIGINAL path is for
            # the CLI, which only accepts the exact form Proton stored
            # (typically NFD on macOS)
            remote[protonlib.nfc(rel)] = (size, sha1, rel)
    return remote


def main():
    a = parse_args()

    if a.rescan:
        print("  Regenerating the remote inventory...")
        for f in (a.tsv + ".state", a.tsv + ".queue"):
            if os.path.exists(f):
                os.remove(f)
        if not os.path.exists(a.scanner) and os.sep in a.scanner:
            sys.exit(f"ERROR: {a.scanner} not found. Point at it with --scanner.")
        r = subprocess.run([sys.executable, a.scanner, a.root, "-o", a.tsv,
                            "--cli", a.cli,
                            "--workers", "3", "--delay", "0.2", "--timeout", "300"],
                           stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            print("  WARNING: the scan reported errors: "
                  "the inventory may be incomplete.")

    if not os.path.exists(a.tsv):
        sys.exit(f"ERROR: {a.tsv} doesn't exist. Run proton-scan.py first, "
                 f"or use --rescan.")
    os.makedirs(a.dest, exist_ok=True)

    remote = load_tsv(a.tsv)
    print("")
    print(f"  REMOTE : {a.tsv}  ({len(remote)} files)")
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
        logdir = os.path.expanduser("~/drive-sync-logs")
        os.makedirs(logdir, exist_ok=True)
        p = os.path.join(logdir, "files-not-downloaded.txt")
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
