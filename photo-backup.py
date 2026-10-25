#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-backup.py - Incremental backup of the Proton Drive Photos section.

WHY
  The Photos section is not synced by the desktop client and is not a
  filesystem path: it only exists through the CLI's `photo` commands
  (`photo timeline`, `photo download`). This script downloads it to a local
  folder and remembers what it already fetched, so later runs only copy the
  new photos.

HOW IT WORKS
  1. `photo timeline` produces the full inventory (uid, capture date and,
     with --details, name, size and sha1). It is saved as
     .proton-photo-inventory.json in the destination folder.
  2. uids already downloaded are read from the state file
     (.proton-photo-state.txt). uids no longer on Proton are dropped from it.
  3. missing photos are downloaded in batches, to amortize the ~1.4 s startup
     cost the CLI pays on every invocation, into one folder per year of
     capture.
  4. every downloaded photo is CONFIRMED BY CONTENT before being recorded:
     the sha1 of the new files is compared with the sha1 Proton declares.
     Counting files is not enough: with `-c rename` a file can arrive under a
     different name, and a partly failed batch can look complete. When a
     batch is only partly confirmed, the rest is retried one photo at a time.

  --details is strongly recommended: without it the inventory has no sha1
  and downloads can only be confirmed by counting files.

  Photos that fail are listed in photos-not-downloaded.txt in the
  destination folder; rerun the script to retry only those (or pass
  --skip-failed to leave them out).

  photo-fixdate.py and album-backup.py build on the inventory saved here.

USAGE
  ./photo-backup.py /path/to/backup/proton-photos --details
  ./photo-backup.py /path/to/backup/proton-photos --details --batch 5 --timeout 60
  ./photo-backup.py /path/to/backup/proton-photos --dry-run

  A small --batch (5) is slower in theory but much faster in practice on a
  large library: big batches time out often, and every timeout wastes the
  whole batch.

SAFETY
  Downloads only. Never modifies or deletes anything on Proton.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import protonlib

STATE = ".proton-photo-state.txt"
INVENTORY = ".proton-photo-inventory.json"
FAILED = "photos-not-downloaded.txt"


def parse_args():
    p = argparse.ArgumentParser(
        description="Incremental backup of the Proton Drive Photos section.")
    p.add_argument("dest", help="destination folder (e.g. /path/to/backup/proton-photos)")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI, help="path to the CLI binary")
    p.add_argument("--batch", type=int, default=50,
                   help="photos per CLI invocation (default 50)")
    p.add_argument("--details", action="store_true",
                   help="inventory with names, sizes and sha1 (slower, recommended)")
    p.add_argument("--timeout", type=int, default=900,
                   help="timeout per batch, in seconds (default 900)")
    p.add_argument("--limit", type=int, default=0,
                   help="download at most N photos and stop (0 = all)")
    p.add_argument("--reuse-inventory", action="store_true",
                   help="reuse the saved inventory instead of reading it again")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be downloaded")
    p.add_argument("--skip-failed", action="store_true",
                   help=f"skip the photos listed in {FAILED}")
    return p.parse_args()


def load_inventory(args):
    inv_path = os.path.join(args.dest, INVENTORY)
    if args.reuse_inventory and os.path.exists(inv_path):
        with open(inv_path, encoding="utf-8") as f:
            data = json.load(f)
        print(f"  Inventory reused: {len(data)} photos")
        return data

    print("  Reading the inventory from Proton...")
    t0 = time.time()
    cmd = ["photo", "timeline", "-j"]
    if args.details:
        cmd.insert(2, "-d")
    try:
        r = protonlib.run_cli(args.cli, cmd, timeout=3600)
    except subprocess.TimeoutExpired:
        sys.exit("ERROR: 'photo timeline' didn't answer within an hour.")
    if r.returncode != 0:
        sys.exit(f"ERROR from 'photo timeline': {(r.stderr or r.stdout)[:400]}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: 'photo timeline' output is not valid JSON: {e}")

    print(f"  {len(data)} photos in the inventory ({int(time.time()-t0)}s)")
    with open(inv_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def uid_of(item):
    return item.get("nodeUid") or item.get("uid")


def year_of(item):
    # order of reliability: the CAPTURE date first. claimedModificationTime
    # is often the date of a later import and puts photos in the wrong year.
    ph = item.get("photo") or {}
    rev = item.get("activeRevision") or {}
    cam = (rev.get("claimedAdditionalMetadata") or {}).get("Camera") or {}
    ts = (ph.get("captureTime") or cam.get("CaptureTime")
          or item.get("captureTime")
          or rev.get("claimedModificationTime") or item.get("creationTime"))
    if not ts:
        return "no-date"
    try:
        return str(datetime.fromisoformat(ts.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).year)
    except Exception:
        return "no-date"


def load_state(dest):
    p = os.path.join(dest, STATE)
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8") as f:
        return set(l.strip() for l in f if l.strip())


def main():
    a = parse_args()
    a.dest = os.path.expanduser(a.dest)
    os.makedirs(a.dest, exist_ok=True)

    print("")
    print(f"  Destination : {a.dest}")
    print(f"  Batch       : {a.batch} photos per invocation")
    print("")

    inventory = load_inventory(a)

    # sha1 declared by Proton for each photo: used to confirm downloads by
    # CONTENT instead of by the number of files that showed up in the folder.
    expected_sha = {}
    for x in inventory:
        u = uid_of(x)
        h = ((x.get("activeRevision") or {}).get("claimedDigests") or {}).get("sha1")
        if u and h:
            expected_sha[u] = h
    if expected_sha:
        print(f"  Content verification on ({len(expected_sha)} sha1 available)")
    else:
        print("  WARNING: inventory without sha1 (--details missing).")
        print("  Downloads will be confirmed by counting files: less reliable.")

    done = load_state(a.dest)

    # The state file keeps the uids already downloaded, but photos trashed on
    # Proton would stay in it forever. Only those still in the inventory are
    # kept, so the count reflects reality and the file doesn't keep growing.
    valid = {u for u in (uid_of(x) for x in inventory) if u}
    obsolete = done - valid
    if obsolete:
        done = done & valid
        with open(os.path.join(a.dest, STATE), "w", encoding="utf-8") as f:
            if done:
                f.write("\n".join(sorted(done)) + "\n")
        print(f"  State file cleaned: {len(obsolete)} uids no longer on Proton")

    print(f"  Already downloaded: {len(done)}")

    skipped = set()
    if a.skip_failed:
        p = os.path.join(a.dest, FAILED)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                skipped = set(l.split("\t")[0].strip() for l in f if l.strip())
            print(f"  Skipped (failed before): {len(skipped)}")
        done = done | skipped

    # group what's missing by year
    todo = {}
    for it in inventory:
        u = uid_of(it)
        if not u or u in done:
            continue
        todo.setdefault(year_of(it), []).append(u)

    total = sum(len(v) for v in todo.values())
    if total == 0:
        print("")
        print("  No new photos: the backup is in sync.")
        return 0

    print(f"  To download : {total}")
    print("")
    for y in sorted(todo):
        print(f"    {y}: {len(todo[y])}")
    print("")

    if a.dry_run:
        print("  (--dry-run: nothing downloaded)")
        return 0

    state_path = os.path.join(a.dest, STATE)
    downloaded = 0
    failed = []
    t0 = time.time()

    for year in sorted(todo, reverse=True):
        uids = todo[year]
        folder = os.path.join(a.dest, year)
        os.makedirs(folder, exist_ok=True)

        for i in range(0, len(uids), a.batch):
            if a.limit and downloaded >= a.limit:
                print(f"\n  Reached the limit of {a.limit} photos.")
                break
            batch = uids[i:i + a.batch]
            before = set(os.listdir(folder))
            cmd = ["photo", "download", "-c", "rename"] \
                + [f"/photos/{u}" for u in batch] + [folder]
            try:
                r = protonlib.run_cli(a.cli, cmd, timeout=a.timeout)
                ok = (r.returncode == 0)
                err = (r.stderr or r.stdout or "").strip()[:200]
            except subprocess.TimeoutExpired:
                ok, err = False, f"timed out after {a.timeout}s"

            new_files = set(os.listdir(folder)) - before
            arrived = len(new_files)

            # which uids really arrived: compare the sha1 of the new files with
            # the one Proton declares for each photo
            confirmed = set()
            if expected_sha:
                new_hashes = set()
                for f in new_files:
                    h = protonlib.sha1_of(os.path.join(folder, f))
                    if h:
                        new_hashes.add(h)
                for u in batch:
                    h = expected_sha.get(u)
                    if h is None or h in new_hashes:
                        confirmed.add(u)
            elif ok and arrived >= len(batch):
                confirmed = set(batch)

            if (not ok) and len(batch) == 1:
                # already a one-photo batch: retrying it alone would be identical
                failed.append((batch[0], err))
                el = int(time.time() - t0)
                sys.stderr.write(f"\r  {year}: {downloaded}/{total} photos | {el}s "
                                 f"| {len(failed)} failed     ")
                sys.stderr.flush()
                continue

            if len(confirmed) == len(batch):
                with open(state_path, "a", encoding="utf-8") as f:
                    f.write("\n".join(batch) + "\n")
                downloaded += len(batch)
            else:
                # record the confirmed ones right away, so the one-by-one
                # retry only covers what is really missing
                if confirmed:
                    with open(state_path, "a", encoding="utf-8") as f:
                        f.write("\n".join(sorted(confirmed)) + "\n")
                    downloaded += len(confirmed)
                # partial or failed batch: we don't know which photos made it,
                # so retry one by one instead of recording them in bulk
                recovered = 0
                for u in [x for x in batch if x not in confirmed]:
                    before_one = set(os.listdir(folder))
                    try:
                        r2 = protonlib.run_cli(a.cli, ["photo", "download", "-c", "rename",
                                                       f"/photos/{u}", folder],
                                               timeout=a.timeout)
                        came = set(os.listdir(folder)) - before_one
                        expected = expected_sha.get(u)
                        if expected:
                            got_it = any(protonlib.sha1_of(os.path.join(folder, f)) == expected
                                         for f in came)
                        else:
                            got_it = bool(came)
                        if r2.returncode == 0 and got_it:
                            with open(state_path, "a", encoding="utf-8") as f:
                                f.write(u + "\n")
                            recovered += 1
                        else:
                            failed.append((u, (r2.stderr or "")[:120]))
                    except subprocess.TimeoutExpired:
                        failed.append((u, "timeout"))
                downloaded += recovered
                if not ok:
                    sys.stderr.write(f"\n  batch failed ({err}); "
                                     f"recovered {recovered}/{len(batch)} one by one\n")

            el = int(time.time() - t0)
            rate = downloaded / el if el else 0
            left = int((total - downloaded) / rate) if rate > 0 else 0
            sys.stderr.write(
                f"\r  {year}: {downloaded}/{total} photos | {el}s | {rate:.1f} photos/s "
                f"| about {left//60} min left    ")
            sys.stderr.flush()
        if a.limit and downloaded >= a.limit:
            break

    sys.stderr.write("\n")
    print("")
    print(f"  Downloaded : {downloaded}")
    print(f"  Failed     : {len(failed)}")
    if failed:
        p = os.path.join(a.dest, FAILED)
        with open(p, "w", encoding="utf-8") as f:
            for u, e in failed:
                f.write(f"{u}\t{e}\n")
        print(f"  List       : {p}")
        print("  Rerun the script: it retries only the missing ones.")
        return 1
    print("")
    print("  Backup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
