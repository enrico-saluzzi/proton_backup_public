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
  1. scans the remote folder (--root, default /my-files) through the CLI, one
     call per folder: about a second per folder, half an hour or more on a
     large Drive. The scan is saved in ~/drive-sync-logs; if it gets
     interrupted, run the same command again and it resumes; --reuse-scan
     uses the last complete one.
  2. compares it with the files in the backup folder
  3. downloads what is missing and what has a different size
  4. verifies (verify = yes, the default): recomputes the local sha1 and
     re-downloads any mismatch
  5. cleans up (cleanup = yes, the default), only when every download
     succeeded, the files in the backup that are no longer on Proton:
       - moved or renamed on Proton: the old copy is deleted, once the same
         content is in the backup at its new path (checked by sha1)
       - deleted on Proton: moved to deleted_dir/<date>/, with their path,
         in case the deletion was a mistake. deleted_dir is by default
         deleted-on-proton next to the backup folder (e.g.
         /Volumes/Disk/deleted-on-proton for /Volumes/Disk/proton-files), so
         it's easy to browse in Finder; when the backup folder is the root
         of a disk, set it in the profile (--deleted-dir). The script never
         empties it: delete what you no longer need.
     Folders left empty are removed. If more than 10% of the backup would
     go, it stops (wrong folder or proton_root?) unless --force-cleanup.

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
  (verify = yes): turn it off with verify = no or --no-verify. So is the
  cleanup (cleanup = yes): turn it off with cleanup = no or --no-cleanup.

USAGE
  ./file-backup.py --setup ssd
  ./file-backup.py                       # monthly, with the only profile
  ./file-backup.py ssd --dry-run
  ./file-backup.py /path/to/backup/proton-files --no-verify
  ./file-backup.py /path/to/backup/work --root /my-files/Work

SAFETY
  Downloads only. Never modifies or deletes anything on Proton.
  Local files that no longer exist on Proton are only reported, unless
  cleanup = yes (see step 5): even then a file is deleted only when its
  content is still in the backup, and the rest is moved aside, not deleted.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

import protonlib


SCRIPT = "file-backup"
HELP = {k: h for k, _, h in protonlib.PROFILE_SETTINGS[SCRIPT]}
# where cleanup moves the files deleted on Proton, next to the backup folder
# unless deleted_dir says otherwise
DELETED_DIR = "deleted-on-proton"
# cleanup stops when it would take away more than this share of the backup
CLEANUP_MAX = 0.10


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
    c = g.add_mutually_exclusive_group()
    c.add_argument("--cleanup", dest="cleanup", action="store_const", const="yes",
                   help="remove from the backup what is no longer on Proton, see step 5 "
                        "(the default)")
    c.add_argument("--no-cleanup", dest="cleanup", action="store_const", const="no",
                   help="only report what is no longer on Proton")
    g.add_argument("--deleted-dir",
                   help="where cleanup moves the files deleted on Proton "
                        "(default: deleted-on-proton next to the backup folder)")
    g.add_argument("--workers", type=int, help="parallel calls while scanning (default 3)")
    g.add_argument("--batch", type=int, help="files per download call (default 20)")
    g.add_argument("--timeout", type=int, help="timeout per download call, in seconds (default 1800)")
    p.add_argument("--reuse-scan", action="store_true",
                   help="reuse the last complete scan instead of scanning Proton again")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI, help="path to the CLI binary")
    p.add_argument("--force-cleanup", action="store_true",
                   help="clean up even when more than 10%% of the backup would go")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be downloaded and cleaned up")
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
    for k in ("root", "verify", "cleanup", "deleted_dir", "workers", "batch", "timeout"):
        v = getattr(a, k)
        if v is not None:
            s["proton_root" if k == "root" else k] = str(v)
    protonlib.check_settings(s, where)

    a.profile = name
    a.where = where
    a.dest = os.path.expanduser(s["dest"])
    a.root = s["proton_root"]
    a.verify = s["verify"] == "yes"
    a.cleanup = s["cleanup"] == "yes"
    a.deleted_dir = os.path.expanduser(s["deleted_dir"]) or os.path.join(
        os.path.dirname(os.path.abspath(a.dest).rstrip(os.sep)), DELETED_DIR)
    a.workers, a.batch, a.timeout = s["workers"], s["batch"], s["timeout"]
    # a profile's backup folder must already exist: a missing one usually
    # means the external disk isn't plugged in, and creating it would fill
    # the internal disk instead
    if name is not None and not os.path.isdir(a.dest):
        sys.exit(f"ERROR: {a.dest} doesn't exist ({where}). Is the disk plugged in?")
    # next to the root of a disk would mean another disk (/Volumes)
    if a.cleanup and not s["deleted_dir"] and os.path.ismount(a.dest):
        sys.exit(f"ERROR: {a.dest} is the root of a disk, so cleanup has no folder next "
                 f"to it for the files deleted on Proton ({where}).\n"
                 "  Set deleted_dir (--deleted-dir), e.g. a folder on the same disk.")
    return a


def setup_questions(cur):
    ask = protonlib.ask
    new = {}
    print("")
    print(f"  {HELP['dest']}")
    new["dest"] = protonlib.ask_existing_folder("dest", cur["dest"] or "/Volumes/MyBackupDisk/proton-files")
    for k in ("proton_root", "verify", "cleanup", "deleted_dir", "workers", "batch", "timeout"):
        print("")
        print(f"  {HELP[k]}")
        new[k] = ask(k, cur[k], protonlib.SETTING_CHOICES.get(k), protonlib.SETTING_MINIMUM.get(k))
    return new


def get_scan(a):
    """Path of an up-to-date, complete scan of a.root."""
    tsv = protonlib.scan_path(a.root)
    if a.reuse_scan:
        if not protonlib.scan_complete(tsv):
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


def local_path(dest, rel):
    """Where the backup keeps the remote file rel, or None if it's missing."""
    for path in (os.path.join(dest, rel), os.path.join(dest, protonlib.as_local(rel))):
        if os.path.exists(path):
            return path
    return None


def backup_files(dest, deleted_dir):
    """Files in the backup: normalized relative path -> relative path on disk.
    Hidden files and deleted_dir (if it's inside the backup) are left out."""
    files = {}
    skip = os.path.realpath(deleted_dir)
    for dirpath, dirnames, filenames in os.walk(dest):
        dirnames[:] = [d for d in dirnames
                       if os.path.realpath(os.path.join(dirpath, d)) != skip]
        for fn in filenames:
            if fn.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), dest)
            files[protonlib.nfc(rel)] = rel
    return files


def plan_cleanup(a, remote, on_disk, local_only, pending):
    """Splits the files only in the backup into old copies of files moved on
    Proton, [(rel, new rel)], and files deleted on Proton, [rel]. A file
    counts as moved only when a remote file with the same sha1 is in the
    backup with that content, or is about to be downloaded (pending, for
    --dry-run)."""
    by_sha1 = {}
    for rel, (_, sha1, _) in remote.items():
        if sha1 and sha1 != "-":
            by_sha1.setdefault(sha1, []).append(rel)
    checked = {}

    def in_backup(rel, sha1):
        if rel in pending:
            return True
        if rel not in checked:
            path = local_path(a.dest, rel)
            checked[rel] = path is not None and protonlib.sha1_of(path) == sha1
        return checked[rel]

    moved, deleted = [], []
    for rel in local_only:
        h = protonlib.sha1_of(os.path.join(a.dest, on_disk[rel]))
        new = next((r for r in by_sha1.get(h, []) if in_backup(r, h)), None)
        if new:
            moved.append((rel, new))
        else:
            deleted.append(rel)
    return moved, deleted


def remove_empty_dirs(dest, folder):
    """Removes folder and its parents up to dest while they are empty (a
    Finder .DS_Store doesn't count)."""
    while folder != dest and folder.startswith(dest + os.sep):
        try:
            if os.listdir(folder) == [".DS_Store"]:
                os.remove(os.path.join(folder, ".DS_Store"))
            os.rmdir(folder)
        except OSError:
            return
        folder = os.path.dirname(folder)


def free_path(path):
    """path, or path with " (2)", " (3)"... before the extension if it exists."""
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.lexists(path):
        n += 1
        path = f"{base} ({n}){ext}"
    return path


def cleanup(a, remote, on_disk, local_only, pending):
    """Step 5: deletes the old copies of files moved on Proton and moves the
    files deleted on Proton to deleted_dir/<date>/. Returns the number of
    files it couldn't handle."""
    moved, deleted = plan_cleanup(a, remote, on_disk, local_only, pending)
    hold = os.path.join(a.deleted_dir, time.strftime("%Y-%m-%d"))
    print("")
    if moved:
        print(f"  Moved on Proton, old copy deleted: {len(moved)}")
        for old, new in moved[:a.max_print]:
            print(f"     {old}")
            print(f"       -> {new}")
        if len(moved) > a.max_print:
            print(f"     ... and {len(moved) - a.max_print} more")
    if deleted:
        print(f"  Deleted on Proton, moved to {hold}: {len(deleted)}")
        for rel in deleted[:a.max_print]:
            print(f"     {rel}")
        if len(deleted) > a.max_print:
            print(f"     ... and {len(deleted) - a.max_print} more")
    if a.dry_run:
        print("")
        print("  (--dry-run: nothing cleaned up)")
        return 0

    errors = []
    for rel, _ in moved:
        path = os.path.join(a.dest, on_disk[rel])
        try:
            os.remove(path)
        except OSError as e:
            errors.append((rel, e))
            continue
        remove_empty_dirs(a.dest, os.path.dirname(path))
    for rel in deleted:
        path = os.path.join(a.dest, on_disk[rel])
        target = free_path(os.path.join(hold, on_disk[rel]))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(path, target)
        except OSError as e:
            errors.append((rel, e))
            continue
        remove_empty_dirs(a.dest, os.path.dirname(path))
    for rel, e in errors:
        print(f"  ERROR cleaning up {rel}: {e}")
    return len(errors)


def report_hold(root):
    """Reminds what the cleanup has moved aside so far."""
    count = size = 0
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if not fn.startswith("."):
                count += 1
                size += os.path.getsize(os.path.join(dirpath, fn))
    if count:
        print("")
        print(f"  Deleted on Proton, kept aside: {count} files ({protonlib.human(size)}) in {root}")
        print("  Delete what you no longer need: the script never empties it.")


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
        local = local_path(a.dest, rel)
        if local is None:
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
    on_disk = backup_files(a.dest, a.deleted_dir)
    expected = set(remote) | {protonlib.nfc(protonlib.as_local(r)) for r in remote}
    local_only = sorted(set(on_disk) - expected)

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

    if local_only and a.cleanup and not a.force_cleanup \
            and len(local_only) > max(10, CLEANUP_MAX * len(on_disk)):
        sys.exit(f"\nERROR: cleanup would take {len(local_only)} of the {len(on_disk)} files "
                 f"out of the backup (more than {CLEANUP_MAX:.0%}).\n"
                 f"  Check the backup folder ({a.dest}) and proton_root ({a.root}).\n"
                 "  If they are right, run again with --force-cleanup; with --no-cleanup "
                 "it backs up without cleaning up.")

    if local_only and not a.cleanup:
        print("")
        print("  In the backup but no longer on Proton (NOT touched, cleanup = no):")
        for r in local_only[:a.max_print]:
            print("     " + r)
        if len(local_only) > a.max_print:
            print(f"     ... and {len(local_only) - a.max_print} more")

    failed = 0
    if not to_download:
        print("")
        print("  Backup in sync: nothing to download.")
    else:
        failed = download(a, remote, to_download, bytes_to_download)
    if failed:
        if a.cleanup and local_only:
            print("  Cleanup skipped: it runs only when every download succeeded.")
        return 1
    if a.cleanup and local_only:
        pending = {r for r, _ in to_download} if a.dry_run else set()
        if cleanup(a, remote, on_disk, local_only, pending):
            return 1
    report_hold(a.deleted_dir)
    return 0


def download(a, remote, to_download, bytes_to_download):
    """Steps 3-4: downloads the missing and different files. Returns the
    number of files that failed."""
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
        return len(failed)
    print("")
    print("  Backup in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
