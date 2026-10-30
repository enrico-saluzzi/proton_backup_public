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
  4. verifies (verify = yes, the default): recomputes the local sha1 and
     re-downloads any mismatch

  Files are downloaded in batches grouped by destination folder, because the
  CLI downloads into one folder per call. Batches that fail are listed in
  ~/drive-sync-logs/files-not-downloaded.txt; just run again and it retries
  only what is still missing.

  Note: Proton reports these hashes as `sha1Verified: false`, i.e. declared by
  the uploading client, so --verify confirms that the backup matches what
  Proton *declares* to hold.

PROFILES
  The backup folder and the options live in a profile, in the same
  configuration file as drive-compare.py: ~/.config/proton-backup/config.ini
  (another one with --config), section [file-backup <name>]. Create or
  change one with:

      ./file-backup.py --setup ssd

  then back up with ./file-backup.py ssd, or just ./file-backup.py when it's
  the only file-backup profile. A folder path works too, with the defaults.
  Command-line options override the profile. --verify is on by default
  (verify = yes): turn it off with verify = no or --no-verify.

USAGE
  ./file-backup.py --setup ssd
  ./file-backup.py                       # monthly, with the only profile
  ./file-backup.py ssd --dry-run
  ./file-backup.py /path/to/backup/proton-files --no-verify
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


SCRIPT = "file-backup"
HELP = {k: h for k, _, h in protonlib.PROFILE_SETTINGS[SCRIPT]}


def parse_args():
    p = argparse.ArgumentParser(
        description="Incremental backup of Proton Drive files.",
        epilog="Settings: ~/.config/proton-backup/config.ini (see config.example.ini). "
               "Options given here override the profile.")
    p.add_argument("target", nargs="?",
                   help="a profile from the configuration file (optional when there's "
                        "only one), or the backup folder")
    p.add_argument("--setup", action="store_true",
                   help="create or change the profile named by TARGET, asking questions")
    p.add_argument("--config", default=protonlib.CONFIG_PATH,
                   help="configuration file (default ~/.config/proton-backup/config.ini)")
    g = p.add_argument_group("settings (override the profile)")
    g.add_argument("--root", help="remote folder to back up (default /my-files, the whole Drive)")
    v = g.add_mutually_exclusive_group()
    v.add_argument("--verify", dest="verify", action="store_const", const="yes",
                   help="recompute the sha1 of every local file (the default)")
    v.add_argument("--no-verify", dest="verify", action="store_const", const="no",
                   help="trust name and size: faster, misses silent corruption")
    g.add_argument("--workers", type=int, help="parallel calls while scanning (default 3)")
    g.add_argument("--batch", type=int, help="files per download call (default 20)")
    g.add_argument("--timeout", type=int, help="timeout per download call, in seconds (default 1800)")
    p.add_argument("--reuse-scan", action="store_true",
                   help="reuse the last complete scan instead of scanning Proton again")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI, help="path to the CLI binary")
    p.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    p.add_argument("--max-print", type=int, default=25)
    a = p.parse_args()
    a.config = os.path.expanduser(a.config)
    return a


def resolve_settings(a):
    """Settings for this run: profile (or defaults), then command-line options.
    Copies them onto `a` (a.dest, a.root, a.verify, ...)."""
    cfg = protonlib.read_config(a.config)
    name = protonlib.choose_profile(cfg, SCRIPT, a.target, a.config)
    s = protonlib.load_settings(cfg, SCRIPT, name)
    if name is None:
        s["dest"] = a.target
        where = "defaults and command line"
    else:
        where = f"profile [{protonlib.profile_section(SCRIPT, name)}] in {a.config}"
        if not s["dest"]:
            sys.exit(f"ERROR: {where} has no dest. Run: ./{SCRIPT}.py --setup {name}")
    for k in ("root", "verify", "workers", "batch", "timeout"):
        v = getattr(a, k)
        if v is not None:
            s["proton_root" if k == "root" else k] = str(v)
    protonlib.check_settings(s, where)

    a.profile = name
    a.where = where
    a.dest = os.path.expanduser(s["dest"])
    a.root = s["proton_root"]
    a.verify = s["verify"] == "yes"
    a.workers, a.batch, a.timeout = s["workers"], s["batch"], s["timeout"]
    # a profile's backup folder must already exist: a missing one usually
    # means the external disk isn't plugged in, and creating it would fill
    # the internal disk instead
    if name is not None and not os.path.isdir(a.dest):
        sys.exit(f"ERROR: {a.dest} doesn't exist ({where}). Is the disk plugged in?")
    return a


def setup_questions(cur):
    ask = protonlib.ask
    new = {}
    print("")
    print(f"  {HELP['dest']}")
    new["dest"] = protonlib.ask_existing_folder("dest", cur["dest"] or "/Volumes/MyBackupDisk/proton-files")
    for k in ("proton_root", "verify", "workers", "batch", "timeout"):
        print("")
        print(f"  {HELP[k]}")
        new[k] = ask(k, cur[k], protonlib.SETTING_CHOICES.get(k), protonlib.SETTING_MINIMUM.get(k))
    return new


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
    if a.setup:
        return protonlib.run_setup(a.config, SCRIPT, a.target, setup_questions)
    resolve_settings(a)
    print("")
    tsv = get_scan(a)
    os.makedirs(a.dest, exist_ok=True)

    remote = load_remote(tsv)
    print("")
    print(f"  PROTON : {a.root}  ({len(remote)} files)")
    print(f"  BACKUP : {a.dest}")
    print(f"  FROM   : {a.where}" + ("" if a.verify else "   (no sha1 verification)"))
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
