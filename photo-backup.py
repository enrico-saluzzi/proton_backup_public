#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-backup.py - Incremental backup of Proton Photos: one folder per capture
year, correct file dates, and optionally the albums.

WHY
  The Photos section isn't synced by the desktop client and isn't a
  filesystem path: it only exists through the CLI's `photo` and `album`
  commands. This script keeps a local copy of it, and only fetches what's new
  on each run.

WHAT EACH RUN DOES
  1. reads the timeline (`photo timeline -d -j`): every photo with its capture
     date and sha1. Saved as .proton-photo-inventory.json.
  2. with --import DIR, first adds photos you had to download by hand from the
     web interface (typically iOS edits, "_modified"), matched by name.
  3. hashes the local files and matches them to Proton photos by CONTENT,
     never by name: the CLI renames duplicates, and identical content can exist
     under several uids.
  4. downloads what's missing into the folder of its capture year, in batches
     (the CLI pays ~1.4 s of startup per call). Every download is confirmed by
     sha1, not by counting files: a partly failed batch is retried one photo
     at a time.
  5. sets each file's date to the capture date and moves files into the right
     year folder if needed.
  6. with --albums, rebuilds every album as a folder of HARD LINKS: a second,
     album-based view of the same files, using no extra space.
  7. cleans up (cleanup = yes, the default), only when every download
     succeeded, so that the backup follows Proton:
       - photos deleted on Proton (local files whose content is on Proton
         no more, e.g. the old versions replaced by photo-redate.py): moved
         to deleted_dir/<date>/<year>/, in case the deletion was a mistake.
         A file whose name matches a Proton photo that has no file in the
         backup is kept instead: it may be a converted copy (--import).
       - extra copies of a photo already in the backup (same sha1): deleted,
         after pointing the album links that used them to the copy that
         stays (so the space is really freed)
       - with albums: links of photos no longer in their album, and folders
         of albums deleted on Proton, are removed. A link is only a second
         name of a photo file; if it was the last copy of its content, it's
         moved to deleted_dir/<date>/<album folder>/ instead.
     deleted_dir is by default deleted/photos next to the backup folder
     (for /Volumes/Disk/proton-photos: /Volumes/Disk/deleted/photos, while
     file-backup.py uses /Volumes/Disk/deleted/files); the script never
     empties it.
     It stops if more than 10% of the backup would go (unless
     --force-cleanup).
  8. saves the uid -> file map (.proton-photo-map.json), used by
     photo-prune.py and photo-redate.py, and reports:
     - photos deleted from the backup but still on Proton. Deleted on
       purpose? photo-prune.py --from-missing trashes them on Proton.
       By accident? --redownload-missing gets them back.
     - local files that match no Proton photo (unrecognized-photos.txt).

  The run can be interrupted at any time: downloads are recorded one by one
  in .proton-photo-state.txt and the next run picks up from there. Photos
  that fail are listed in photos-not-downloaded.txt and retried next time
  (--skip-failed leaves them out).

ALBUMS
  Album folders go in album_dir (--album-dir; default: "album" inside the
  backup folder); they must be on the same volume as the photos (symlinks are
  used otherwise). Two different photos of an album with the same file name both
  appear, the second with a " (1)" suffix. With cleanup = no, album folders
  only grow: to rebuild them cleanly, delete the album folder and run again.

PROFILES
  The backup folder and the options live in a profile, in the same
  configuration file as file-backup.py and drive-compare.py:
  ~/.config/proton-backup/config.ini (another one with --config), section
  [photo-backup <name>]. Create or change one with:

      ./photo-backup.py --setup ssd

  then back up with ./photo-backup.py ssd, or just ./photo-backup.py when
  it's the only photo-backup profile. A folder path works too, with the
  defaults. Command-line options override the profile; the per-run ones
  (--import, --redownload-missing, --limit, --reuse-scan,
  --skip-failed, --force-cleanup, --dry-run) only exist on the command line.

USAGE
  ./photo-backup.py --setup ssd
  ./photo-backup.py                     # monthly, with the only profile
  ./photo-backup.py --import ~/Downloads
  ./photo-backup.py ssd --dry-run
  ./photo-backup.py /path/to/backup/proton-photos --albums --album-dir /path/to/backup/proton-albums

SAFETY
  Never modifies or deletes anything on Proton. Locally it adds files, sets
  dates, moves files between year folders and creates album links. With
  cleanup = yes (step 7) it deletes a file or a link only when the same
  content stays in the backup; everything else goes to deleted_dir.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import protonlib

STATE = ".proton-photo-state.txt"
INVENTORY = ".proton-photo-inventory.json"
PHOTO_MAP = ".proton-photo-map.json"
ALBUM_MAP = ".proton-albums.json"
FAILED = "photos-not-downloaded.txt"
UNRECOGNIZED = "unrecognized-photos.txt"
DELETED_LOCALLY = "photos-deleted-locally.txt"
WORK_DIRS = {"_reupload"}       # photo-redate.py's work folder
# where cleanup moves the photos deleted on Proton, next to the backup folder
# unless deleted_dir says otherwise
DELETED_DIR = os.path.join("deleted", "photos")


SCRIPT = "photo-backup"
HELP = {k: h for k, _, h in protonlib.PROFILE_SETTINGS[SCRIPT]}


def parse_args():
    p = argparse.ArgumentParser(
        description="Incremental backup of Proton Photos, by capture year, with albums.",
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
    al = g.add_mutually_exclusive_group()
    al.add_argument("--albums", dest="albums", action="store_const", const="yes",
                    help="also rebuild the albums as folders of hard links")
    al.add_argument("--no-albums", dest="albums", action="store_const", const="no",
                    help="skip the albums this time")
    g.add_argument("--album-dir",
                   help="where the album folders go: relative to the backup folder or "
                        "absolute (default: album)")
    c = g.add_mutually_exclusive_group()
    c.add_argument("--cleanup", dest="cleanup", action="store_const", const="yes",
                   help="remove from the backup what is no longer on Proton, see step 7 "
                        "(the default)")
    c.add_argument("--no-cleanup", dest="cleanup", action="store_const", const="no",
                   help="only report what is no longer on Proton")
    g.add_argument("--deleted-dir",
                   help="where cleanup moves the photos deleted on Proton "
                        "(default: deleted/photos next to the backup folder)")
    g.add_argument("--batch", type=int,
                   help="photos per CLI call (default 5: bigger batches time out more)")
    g.add_argument("--timeout", type=int,
                   help="timeout per CLI call, in seconds (default 300)")
    p.add_argument("--import", dest="import_dir", default=None, metavar="DIR",
                   help="first add photos downloaded by hand into DIR (moved, matched by name)")
    p.add_argument("--redownload-missing", action="store_true",
                   help="download again photos deleted from the backup but still on Proton")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI, help="path to the CLI binary")
    p.add_argument("--limit", type=int, default=0,
                   help="download at most N photos and stop (0 = all)")
    p.add_argument("--reuse-scan", action="store_true",
                   help="reuse the timeline and album list saved last time instead of "
                        "reading them from Proton")
    p.add_argument("--skip-failed", action="store_true",
                   help=f"don't retry the photos listed in {FAILED}")
    p.add_argument("--force-cleanup", action="store_true",
                   help="clean up even when more than 10%% of the backup would go")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be downloaded, changed, imported and cleaned up")
    p.add_argument("--max-print", type=int, default=25)
    a = p.parse_args()
    a.config = os.path.expanduser(a.config)
    if a.import_dir:
        a.import_dir = os.path.expanduser(a.import_dir)
    return a


def resolve_settings(a):
    """Settings for this run: profile (or defaults), then command-line options.
    Copies them onto `a` (a.dest, a.albums, a.album_base, a.batch, a.timeout)."""
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
    for k in ("albums", "album_dir", "cleanup", "deleted_dir", "batch", "timeout"):
        v = getattr(a, k)
        if v is not None:
            s[k] = str(v)
    protonlib.check_settings(s, where)

    a.profile = name
    a.where = where
    a.dest = os.path.expanduser(s["dest"])
    a.albums = s["albums"] == "yes"
    a.album_dir = s["album_dir"]
    a.album_base = os.path.join(a.dest, os.path.expanduser(s["album_dir"]))
    a.cleanup = s["cleanup"] == "yes"
    a.batch, a.timeout = s["batch"], s["timeout"]
    # a profile's backup folder must already exist: a missing one usually
    # means the external disk isn't plugged in, and creating it would fill
    # the internal disk instead
    if name is not None and not os.path.isdir(a.dest):
        sys.exit(f"ERROR: {a.dest} doesn't exist ({where}). Is the disk plugged in?")
    a.deleted_dir = protonlib.deleted_dir(a.dest, s["deleted_dir"], DELETED_DIR,
                                          a.cleanup, where)
    return a


def setup_questions(cur):
    ask = protonlib.ask
    new = {}
    print("")
    print(f"  {HELP['dest']}")
    new["dest"] = protonlib.ask_existing_folder(
        "dest", cur["dest"] or "/Volumes/MyBackupDisk/proton-photos")
    for k in ("albums", "album_dir", "cleanup", "deleted_dir", "batch", "timeout"):
        if (k == "album_dir" and new["albums"] == "no"
                or k == "deleted_dir" and new["cleanup"] == "no"):
            continue
        print("")
        print(f"  {HELP[k]}")
        new[k] = ask(k, cur[k], protonlib.SETTING_CHOICES.get(k),
                     protonlib.SETTING_MINIMUM.get(k))
    return new


# --- Proton inventory --------------------------------------------------------

def uid_of(x):
    return x.get("nodeUid") or x.get("uid")


def name_of(x):
    return (x.get("name") or {}).get("value") or ""


def sha_of_item(x):
    return ((x.get("activeRevision") or {}).get("claimedDigests") or {}).get("sha1")


def capture_time(x):
    """Capture date, with fallbacks in order of reliability. The modification
    time is often the date of a later import, so it comes last."""
    ph = x.get("photo") or {}
    rev = x.get("activeRevision") or {}
    cam = (rev.get("claimedAdditionalMetadata") or {}).get("Camera") or {}
    return (ph.get("captureTime") or cam.get("CaptureTime") or x.get("captureTime")
            or rev.get("claimedModificationTime") or x.get("creationTime"))


def capture_dt(x):
    ts = capture_time(x)
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def year_of(x):
    dt = capture_dt(x)
    return str(dt.astimezone(timezone.utc).year) if dt else "no-date"


def read_timeline(a):
    path = os.path.join(a.dest, INVENTORY)
    if a.reuse_scan and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        print(f"  Timeline reused: {len(data)} photos")
        return data
    protonlib.suggest_reuse(path, "The timeline and album list saved in the backup",
                            "--reuse-scan")
    print("  Reading the timeline from Proton...")
    t0 = time.time()
    try:
        r = protonlib.run_cli(a.cli, ["photo", "timeline", "-d", "-j"], timeout=3600)
    except FileNotFoundError:
        sys.exit(f"ERROR: '{a.cli}' not found. Use --cli to point at it.")
    except subprocess.TimeoutExpired:
        sys.exit("ERROR: 'photo timeline' didn't answer within an hour.")
    if r.returncode != 0:
        sys.exit(f"ERROR from 'photo timeline': {(r.stderr or r.stdout)[:400]}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: 'photo timeline' output is not valid JSON: {e}")
    print(f"  {len(data)} photos on Proton ({int(time.time() - t0)}s)")
    if not a.dry_run:
        protonlib.write_json(path, data)
    return data


def read_albums(a):
    """[{uid, name, photoCount, photos: [uid...], error}] for every album."""
    path = os.path.join(a.dest, ALBUM_MAP)
    if a.reuse_scan and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            albums = json.load(f)
        print(f"  Album list reused: {len(albums)} albums")
        return albums
    print("  Reading the albums from Proton...")
    r = protonlib.run_cli(a.cli, ["album", "list", "-j"], a.timeout)
    if r.returncode != 0:
        sys.exit(f"ERROR from 'album list': {(r.stderr or r.stdout)[:300]}")
    try:
        listed = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: 'album list' didn't return valid JSON: {e}")
    albums = []
    t0 = time.time()
    for i, alb in enumerate(listed, 1):
        uid = alb.get("uid")
        name = (alb.get("name") or {}).get("value") or f"album-{uid[-8:] if uid else i}"
        expected = (alb.get("album") or {}).get("photoCount", 0)
        sys.stderr.write(f"\r  {i}/{len(listed)}  {name[:44]:<44}")
        sys.stderr.flush()
        try:
            r = protonlib.run_cli(a.cli, ["album", "photos", "-j", f"/albums/{uid}"], a.timeout)
            photos = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
            err = None if r.returncode == 0 else (r.stderr or "")[:150]
        except Exception as e:
            photos, err = [], str(e)[:150]
        uids = [f.get("nodeUid") for f in photos if f.get("nodeUid")]
        albums.append({"uid": uid, "name": name, "photoCount": expected,
                       "coverPhotoNodeUid": (alb.get("album") or {}).get("coverPhotoNodeUid"),
                       "photos": uids, "creationTime": alb.get("creationTime"), "error": err})
        if err or (expected and len(uids) != expected):
            sys.stderr.write(f"\n    WARNING: {name}: expected {expected}, read {len(uids)}"
                             f"{' - ' + err if err else ''}\n")
    sys.stderr.write("\r" + " " * 70 + "\r")
    print(f"  {len(albums)} albums read ({int(time.time() - t0)}s)")
    if not a.dry_run:
        protonlib.write_json(path, albums, ensure_ascii=False, indent=1)
    return albums


# --- local files -------------------------------------------------------------

def list_local(a):
    """Every photo file in the backup, outside album, work and deleted_dir
    folders."""
    out = []
    skip = {os.path.realpath(a.album_base), os.path.realpath(a.deleted_dir)}
    for dp, dn, fn in os.walk(a.dest):
        dn[:] = [d for d in dn if not d.startswith(".") and d not in WORK_DIRS
                 and os.path.realpath(os.path.join(dp, d)) not in skip]
        for f in fn:
            if f.startswith(".") or f.endswith(".txt") or f.endswith(".json"):
                continue
            out.append(os.path.join(dp, f))
    return out


def hash_files(paths, label):
    hashes = {}
    t0 = time.time()
    for i, p in enumerate(paths, 1):
        h = protonlib.sha1_of(p)
        if h:
            hashes[p] = h
        if i % 250 == 0 or i == len(paths):
            sys.stderr.write(f"\r  {label}: {i}/{len(paths)} files | {int(time.time() - t0)}s   ")
            sys.stderr.flush()
    if paths:
        sys.stderr.write("\n")
    return hashes


def same_name(filename, proton_name):
    """'IMG_1 (2).jpg' counts as 'IMG_1.jpg': the CLI adds that suffix on clashes."""
    stem, ext = os.path.splitext(filename)
    stem = re.sub(r" \(\d+\)$", "", stem)
    return (stem + ext).lower() == proton_name.lower()


def build_map(inventory, local_hashes):
    """uid -> local file, matched by content. Several photos can share the same
    content (the same picture uploaded twice): each gets its own copy if there
    is one, preferring the copy with its own name; otherwise they share it."""
    files_by_sha = {}
    for p, h in sorted(local_hashes.items()):
        files_by_sha.setdefault(h, []).append(p)
    photo_map = {}
    for h, records in _records_by_sha(inventory).items():
        paths = files_by_sha.get(h)
        if not paths:
            continue
        free = list(paths)
        pending = []
        for x in records:
            # the exact name first: "IMG_1.jpg" over a "IMG_1 (1).jpg" left by
            # a download that ran twice, so that's the copy kept
            hit = next((p for p in free if os.path.basename(p).lower() == name_of(x).lower()),
                       None) or next((p for p in free
                                      if same_name(os.path.basename(p), name_of(x))), None)
            if hit:
                photo_map[uid_of(x)] = hit
                free.remove(hit)
            else:
                pending.append(x)
        for x in pending:
            photo_map[uid_of(x)] = free.pop(0) if free else paths[0]
    return photo_map


def _records_by_sha(inventory):
    out = {}
    for x in inventory:
        h = sha_of_item(x)
        if h and uid_of(x):
            out.setdefault(h, []).append(x)
    return out


def load_state(a):
    p = os.path.join(a.dest, STATE)
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8") as f:
        return set(l.strip() for l in f if l.strip())


def write_state(a, uids):
    with open(os.path.join(a.dest, STATE), "w", encoding="utf-8") as f:
        if uids:
            f.write("\n".join(sorted(uids)) + "\n")


def append_state(a, uids):
    with open(os.path.join(a.dest, STATE), "a", encoding="utf-8") as f:
        f.write("\n".join(uids) + "\n")


def unique_path(path):
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{stem} ({n}){ext}"
        n += 1
    return path


# --- import of photos downloaded by hand -----------------------------------

def import_manual(a, inventory, done):
    """Move files from --import DIR into the backup when their name matches a
    photo not downloaded yet. Returns the uids imported."""
    if not os.path.isdir(a.import_dir):
        sys.exit(f"ERROR: {a.import_dir} doesn't exist.")
    by_name = {}
    for x in inventory:
        u = uid_of(x)
        if u and u not in done and name_of(x):
            # case-insensitive: the browser may save .MOV where Proton says .mov
            by_name.setdefault(name_of(x).lower(), []).append(x)
    imported = []
    print(f"  Import from {a.import_dir}:")
    for f in sorted(os.listdir(a.import_dir)):
        src = os.path.join(a.import_dir, f)
        if f.startswith(".") or not os.path.isfile(src):
            continue
        rec = by_name.get(f.lower())
        if not rec:
            continue
        if len(rec) > 1:
            print(f"    AMBIGUOUS (several photos with this name, rename it by hand): {f}")
            continue
        x = rec[0]
        expected = sha_of_item(x)
        note = ""
        if expected and protonlib.sha1_of(src) != expected:
            note = "   [different content: the browser may have converted it]"
        year = year_of(x)
        print(f"    {f} -> {year}/{note}")
        if a.dry_run:
            continue
        os.makedirs(os.path.join(a.dest, year), exist_ok=True)
        shutil.move(src, unique_path(os.path.join(a.dest, year, f)))
        imported.append(uid_of(x))
    if imported:
        append_state(a, imported)
        fp = os.path.join(a.dest, FAILED)
        if os.path.exists(fp):
            gone = set(imported)
            with open(fp, encoding="utf-8") as fh:
                rows = [l for l in fh if l.strip() and l.split("\t")[0].strip() not in gone]
            with open(fp, "w", encoding="utf-8") as fh:
                fh.writelines(rows)
    print(f"    {len(imported)} imported")
    return imported


# --- downloads ---------------------------------------------------------------

def download(a, todo, total, expected_sha):
    """Download the missing photos, year by year; returns (count, failed)."""
    downloaded, failed = 0, []
    t0 = time.time()
    for year in sorted(todo, reverse=True):
        uids = todo[year]
        folder = os.path.join(a.dest, year)
        os.makedirs(folder, exist_ok=True)
        for i in range(0, len(uids), a.batch):
            if a.limit and downloaded >= a.limit:
                print(f"\n  Reached the limit of {a.limit} photos.")
                return downloaded, failed
            batch = uids[i:i + a.batch]
            before = set(os.listdir(folder))
            try:
                r = protonlib.run_cli(a.cli, ["photo", "download", "-c", "rename"]
                                      + [f"/photos/{u}" for u in batch] + [folder],
                                      timeout=a.timeout)
                ok, err = r.returncode == 0, (r.stderr or r.stdout or "").strip()[:200]
            except subprocess.TimeoutExpired:
                ok, err = False, f"timed out after {a.timeout}s"

            # which photos really arrived: the sha1 of the new files against the
            # one Proton declares for each photo (a photo without a sha1 can
            # only be confirmed by the call succeeding and a file appearing)
            arrived = set(os.listdir(folder)) - before
            new_hashes = {h for h in (protonlib.sha1_of(os.path.join(folder, f))
                                      for f in arrived) if h}
            confirmed = [u for u in batch
                         if (expected_sha.get(u) in new_hashes if expected_sha.get(u)
                             else ok and arrived)]
            if not confirmed and not ok and len(batch) == 1:
                # already a one-photo batch: retrying it alone would be identical
                failed.append((batch[0], err))
                continue
            if len(confirmed) == len(batch):
                append_state(a, batch)
                downloaded += len(batch)
            else:
                if confirmed:
                    append_state(a, confirmed)
                    downloaded += len(confirmed)
                # partial or failed batch: retry the rest one by one
                recovered = 0
                for u in [x for x in batch if x not in confirmed]:
                    before_one = set(os.listdir(folder))
                    try:
                        r2 = protonlib.run_cli(a.cli, ["photo", "download", "-c", "rename",
                                                       f"/photos/{u}", folder], timeout=a.timeout)
                        came = set(os.listdir(folder)) - before_one
                        expected = expected_sha.get(u)
                        got_it = (any(protonlib.sha1_of(os.path.join(folder, f)) == expected
                                      for f in came) if expected else bool(came))
                        if r2.returncode == 0 and got_it:
                            append_state(a, [u])
                            recovered += 1
                        else:
                            # one line per photo in the failed list
                            failed.append((u, " ".join((r2.stderr or "").split())[:120]))
                    except subprocess.TimeoutExpired:
                        failed.append((u, "timeout"))
                downloaded += recovered
                if not ok:
                    sys.stderr.write(f"\n  batch failed ({err}); "
                                     f"recovered {recovered}/{len(batch)} one by one\n")
            el = int(time.time() - t0)
            rate = downloaded / el if el else 0
            left = int((total - downloaded) / rate) if rate > 0 else 0
            sys.stderr.write(f"\r  {year}: {downloaded}/{total} photos | {el}s | "
                             f"about {left // 60} min left    ")
            sys.stderr.flush()
    sys.stderr.write("\n")
    return downloaded, failed


# --- dates, year folders, albums ----------------------------------------------

def fix_dates(a, photo_map, by_uid):
    """Set each file's date to the capture date and move it into the folder of
    its capture year. Returns (dates set, files moved)."""
    dated = moved = 0
    # photos with identical content may share one file: its date and year come
    # from one owner (preferably the photo with the same name), so the file
    # isn't moved back and forth between years on every run
    owners = {}
    for uid, path in sorted(photo_map.items()):
        cur = owners.get(path)
        if cur is None or (not same_name(os.path.basename(path), name_of(by_uid[cur]))
                           and same_name(os.path.basename(path), name_of(by_uid[uid]))):
            owners[path] = uid
    for path, uid in sorted(owners.items()):
        dt = capture_dt(by_uid[uid])
        if not dt:
            continue
        target_dir = os.path.join(a.dest, str(dt.astimezone(timezone.utc).year))
        if os.path.dirname(path) != target_dir:
            os.makedirs(target_dir, exist_ok=True)
            new = unique_path(os.path.join(target_dir, os.path.basename(path)))
            shutil.move(path, new)
            for u, p in photo_map.items():       # identical content may share the file
                if p == path:
                    photo_map[u] = new
            path = new
            moved += 1
        try:
            os.utime(path, (dt.timestamp(), dt.timestamp()))
            dated += 1
        except OSError:
            pass
    return dated, moved


def same_photo(a_path, b_path):
    """Same file (hard link), or a different file with identical content."""
    if os.path.samefile(a_path, b_path):
        return True
    h = protonlib.sha1_of(a_path)
    return h is not None and h == protonlib.sha1_of(b_path)


def safe_name(name):
    """A name usable as a folder name on macOS."""
    for ch in (":", "/", "\\", "\0"):
        name = name.replace(ch, "_")
    return name.strip() or "unnamed"


def link_albums(a, albums, photo_map):
    """Returns (links created, already present, album photos without a local file)."""
    created = existing = 0
    unlinked = []
    os.makedirs(a.album_base, exist_ok=True)
    for x in albums:
        folder = os.path.join(a.album_base, safe_name(x["name"]))
        os.makedirs(folder, exist_ok=True)
        for u in x["photos"]:
            src = photo_map.get(u)
            if not src or not os.path.exists(src):
                unlinked.append((x["name"], u))
                continue
            # two different photos of the same album can share a file name: a
            # file already there counts as "this photo" only with the same content
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
                    unlinked.append((x["name"], u))
    return created, existing, unlinked


# --- cleanup: the backup follows Proton ---------------------------------------

def base_name(filename):
    """'IMG_1 (2).jpg' -> 'img_1.jpg': the name compared as same_name() does."""
    stem, ext = os.path.splitext(filename)
    return (re.sub(r" \(\d+\)$", "", stem) + ext).lower()


def inode(path):
    st = os.stat(path)
    return st.st_dev, st.st_ino


def album_leftovers(a, albums, photo_map, expected_sha):
    """Files in the album folders that are no longer in their album on Proton,
    or in the folder of an album deleted on Proton. A file stays if it's one
    of the album's photos (same file, or same content). Albums that were read
    incompletely are left alone. Returns (leftovers, files looked at)."""
    wanted, incomplete = {}, set()
    for x in albums:
        folder = safe_name(x["name"])
        wanted.setdefault(folder, set()).update(x["photos"])
        if x.get("error") or (x.get("photoCount") and len(x["photos"]) != x["photoCount"]):
            incomplete.add(folder)
    leftovers, seen = [], 0
    if not os.path.isdir(a.album_base):
        return leftovers, seen
    for folder in sorted(os.listdir(a.album_base)):
        path = os.path.join(a.album_base, folder)
        if folder.startswith(".") or not os.path.isdir(path) or folder in incomplete:
            continue
        uids = wanted.get(folder, set())
        inodes = {inode(photo_map[u]) for u in uids
                  if u in photo_map and os.path.exists(photo_map[u])}
        hashes = {expected_sha[u] for u in uids if u in expected_sha}
        for f in sorted(os.listdir(path)):
            fp = os.path.join(path, f)
            if f.startswith(".") or not os.path.isfile(fp):
                continue
            seen += 1
            if inode(fp) not in inodes and protonlib.sha1_of(fp) not in hashes:
                leftovers.append(fp)
    return leftovers, seen


def plan_cleanup(a, inventory, local_hashes, photo_map, albums, expected_sha):
    """What step 7 does, as lists of paths:
      gone   photo files whose content is no longer on Proton: moved aside
      extra  extra copies of a photo already in the backup: deleted
      kept   content not on Proton, but named like a Proton photo that has no
             file in the backup (a converted copy?): left alone
      links  album files no longer in their album (albums=None: not checked)
    and the number of files looked at."""
    proton = set(expected_sha.values())
    mapped = set(photo_map.values())
    missing_names = {base_name(name_of(x)) for x in inventory if uid_of(x) not in photo_map}
    gone, extra, kept = [], [], []
    for p, h in sorted(local_hashes.items()):
        if p in mapped or not os.path.exists(p):
            continue
        if h in proton:
            extra.append(p)
        elif base_name(os.path.basename(p)) in missing_names:
            kept.append(p)
        else:
            gone.append(p)
    links, seen = ([], 0) if albums is None else album_leftovers(a, albums, photo_map,
                                                                  expected_sha)
    return gone, extra, kept, links, len(local_hashes) + seen


def album_files_by_inode(a):
    """inode -> album files, to find the album links of a photo file."""
    out = {}
    if os.path.isdir(a.album_base):
        for dp, dn, fn in os.walk(a.album_base):
            dn[:] = [d for d in dn if not d.startswith(".")]
            for f in fn:
                p = os.path.join(dp, f)
                if not f.startswith(".") and os.path.isfile(p):
                    out.setdefault(inode(p), []).append(p)
    return out


def relink(link, target):
    """Points the album file `link` to `target` (a hard link), replacing it
    in one step."""
    tmp = link + ".relink"
    os.link(target, tmp)
    os.replace(tmp, link)


def print_list(a, title, paths, base):
    if not paths:
        return
    print(f"  {title}: {len(paths)}")
    for p in paths[:a.max_print]:
        print("     " + os.path.relpath(p, base))
    if len(paths) > a.max_print:
        print(f"     ... and {len(paths) - a.max_print} more")


def cleanup(a, inventory, local_hashes, photo_map, albums, expected_sha):
    """Step 7. Returns False if it stopped or something couldn't be cleaned."""
    gone, extra, kept, links, total = plan_cleanup(a, inventory, local_hashes, photo_map,
                                                   albums, expected_sha)
    hold = os.path.join(a.deleted_dir, time.strftime("%Y-%m-%d"))
    if not (gone or extra or kept or links):
        return True
    print("")
    print_list(a, f"Deleted on Proton, moved to {hold}", gone, a.dest)
    print_list(a, "Extra copies of a photo already in the backup, deleted", extra, a.dest)
    print_list(a, "Album photos no longer in the album, removed", links, a.album_base)
    print_list(a, "Kept: content not on Proton, but named like a photo missing from "
                  "the backup (a converted copy?)", kept, a.dest)
    removed = len(gone) + len(extra) + len(links)
    if a.dry_run:
        print("")
        print("  (--dry-run: nothing cleaned up)")
        return True
    if not a.force_cleanup and protonlib.too_much_to_clean(removed, total):
        print("")
        print(f"  ERROR: cleanup would take {removed} of the {total} files out of the backup "
              f"(more than {protonlib.CLEANUP_MAX:.0%}), so it didn't.")
        print(f"  Check the backup folder ({a.dest}) and the albums folder.")
        print("  If they are right, run again with --force-cleanup.")
        return False

    # content that stays somewhere: an album file with it is just another name
    kept_content = {expected_sha[u] for u in photo_map if u in expected_sha}
    kept_content |= {local_hashes[p] for p in kept}
    errors = []
    stays = {}      # content -> the file the map keeps for it
    for u, p in photo_map.items():
        if u in expected_sha and os.path.exists(p):
            stays.setdefault(expected_sha[u], p)
    album_links = album_files_by_inode(a) if albums is not None and extra else {}
    for p in gone:
        try:
            protonlib.move_aside(p, os.path.join(hold, os.path.relpath(p, a.dest)))
            kept_content.add(local_hashes[p])
            protonlib.remove_empty_dirs(a.dest, os.path.dirname(p))
        except OSError as e:
            errors.append((p, e))
    for p in extra:
        try:
            for link in album_links.get(inode(p), []):
                relink(link, stays[local_hashes[p]])
            os.remove(p)
            protonlib.remove_empty_dirs(a.dest, os.path.dirname(p))
        except OSError as e:
            errors.append((p, e))
    album_dir = os.path.basename(os.path.normpath(a.album_base))
    for p in links:
        try:
            h = protonlib.sha1_of(p)
            if h in kept_content:
                os.remove(p)
            else:       # the last copy of this content: set it aside
                protonlib.move_aside(p, os.path.join(hold, album_dir,
                                                     os.path.relpath(p, a.album_base)))
                kept_content.add(h)
            protonlib.remove_empty_dirs(a.album_base, os.path.dirname(p))
        except OSError as e:
            errors.append((p, e))
    for p, e in errors:
        print(f"  ERROR cleaning up {p}: {e}")
    return not errors


# --- main --------------------------------------------------------------------

def main():
    a = parse_args()
    if a.setup:
        return protonlib.run_setup(a.config, SCRIPT, a.target, setup_questions)
    resolve_settings(a)
    os.makedirs(a.dest, exist_ok=True)
    print("")
    print(f"  Backup : {a.dest}")
    if a.albums:
        print(f"  Albums : {a.album_base}")
    print(f"  From   : {a.where}")
    print("")

    inventory = read_timeline(a)
    by_uid = {uid_of(x): x for x in inventory if uid_of(x)}
    expected_sha = {u: sha_of_item(x) for u, x in by_uid.items() if sha_of_item(x)}
    proton_hashes = set(expected_sha.values())
    if len(expected_sha) < len(by_uid):
        print(f"  {len(by_uid) - len(expected_sha)} photos have no sha1 on Proton: "
              "they can be downloaded but not checked by content.")

    # the state file keeps the uids already downloaded; photos trashed on Proton
    # are dropped from it, so it reflects reality and doesn't grow forever
    done = load_state(a)
    obsolete = done - set(by_uid)
    if obsolete and not a.dry_run:
        done -= obsolete
        write_state(a, done)

    if a.import_dir:
        done |= set(import_manual(a, inventory, done))

    # what's really on disk, by content
    local = list_local(a)
    local_hashes = hash_files(local, "hashing local files")
    on_disk = set(local_hashes.values())

    # recorded as downloaded, still on Proton, but their content is nowhere in
    # the backup: deleted locally (on purpose, or by accident)
    deleted_locally = sorted(u for u in done if u in expected_sha
                             and expected_sha[u] not in on_disk)
    if a.redownload_missing:
        done -= set(deleted_locally)

    skipped = set()
    if a.skip_failed and os.path.exists(os.path.join(a.dest, FAILED)):
        with open(os.path.join(a.dest, FAILED), encoding="utf-8") as f:
            skipped = set(l.split("\t")[0].strip() for l in f if l.strip())

    todo, already_there = {}, []
    for u, x in by_uid.items():
        if u in done or u in skipped:
            continue
        # already in the backup by content (e.g. copied there by hand): just record it
        if expected_sha.get(u) in on_disk:
            already_there.append(u)
            continue
        todo.setdefault(year_of(x), []).append(u)
    if already_there and not a.dry_run:
        append_state(a, already_there)
    total = sum(len(v) for v in todo.values())

    skipped_now = sum(1 for u in by_uid if u in skipped and u not in done)
    print(f"  On Proton: {len(by_uid)}   in the backup: {len(by_uid) - total - skipped_now}   "
          f"to download: {total}"
          + (f"   skipped (failed before): {skipped_now}" if skipped_now else ""))
    for y in sorted(todo):
        print(f"    {y}: {len(todo[y])}")

    if a.dry_run:
        print("")
        print(f"  Deleted from the backup, still on Proton: {len(deleted_locally)}")
        print(f"  Local files matching no Proton photo: "
              f"{sum(1 for h in local_hashes.values() if h not in proton_hashes)}")
        if a.cleanup:
            # albums from the list saved last time: --dry-run doesn't read them
            albums = None
            saved_albums = os.path.join(a.dest, ALBUM_MAP)
            if a.albums and os.path.exists(saved_albums):
                with open(saved_albums, encoding="utf-8") as f:
                    albums = json.load(f)
                print("  (albums compared with the list saved last time: "
                      "--dry-run doesn't read them)")
            cleanup(a, inventory, local_hashes, build_map(inventory, local_hashes),
                    albums, expected_sha)
        print("")
        print("  (--dry-run: nothing downloaded, moved or linked)")
        return 0

    downloaded, failed = (0, [])
    if total:
        print("")
        downloaded, failed = download(a, todo, total, expected_sha)
        new_files = [p for p in list_local(a) if p not in local_hashes]
        local_hashes.update(hash_files(new_files, "hashing new files"))

    photo_map = build_map(inventory, local_hashes)
    dated, moved = fix_dates(a, photo_map, by_uid)

    # keep the last known path of photos deleted locally but still on Proton,
    # so photo-prune.py --from-missing can still find them
    previous = {}
    map_path = os.path.join(a.dest, PHOTO_MAP)
    if os.path.exists(map_path):
        with open(map_path, encoding="utf-8") as f:
            previous = json.load(f)
    still_deleted = [u for u in deleted_locally if u not in photo_map]
    saved_map = dict(photo_map)
    for u in still_deleted:
        if u in previous and not os.path.exists(previous[u]):
            saved_map[u] = previous[u]
    protonlib.write_json(map_path, saved_map, ensure_ascii=False, indent=1)

    unlinked, albums = [], None
    if a.albums:
        print("")
        albums = read_albums(a)
        created, existing, unlinked = link_albums(a, albums, photo_map)

    cleaned = True
    if a.cleanup:
        if failed or (a.limit and downloaded >= a.limit):
            print("")
            print("  Cleanup skipped: it runs only when every download succeeded.")
        else:
            cleaned = cleanup(a, inventory, local_hashes, photo_map, albums, expected_sha)

    unrecognized = sorted(p for p, h in local_hashes.items()
                          if h not in proton_hashes and os.path.exists(p))
    with open(os.path.join(a.dest, UNRECOGNIZED), "w", encoding="utf-8") as f:
        f.write("".join(p + "\n" for p in unrecognized))
    with open(os.path.join(a.dest, DELETED_LOCALLY), "w", encoding="utf-8") as f:
        for u in still_deleted:
            x = by_uid[u]
            f.write(f"{u}\t{name_of(x)}\t{year_of(x)}\n")
    failed_path = os.path.join(a.dest, FAILED)
    if failed:
        with open(failed_path, "w", encoding="utf-8") as f:
            for u, e in failed:
                f.write(f"{u}\t{e}\n")
    elif not a.skip_failed and os.path.exists(failed_path):
        os.remove(failed_path)

    print("")
    print("=" * 66)
    print(f"  Downloaded ..................... {downloaded}")
    if failed:
        print(f"  FAILED ......................... {len(failed)}   (see {FAILED})")
    print(f"  Photos in the backup ........... {len(photo_map)}")
    print(f"  Dates set / moved to their year  {dated} / {moved}")
    if a.albums:
        print(f"  Album links created / present .. {created} / {existing}")
        if unlinked:
            print(f"  Album photos without a file .... {len(unlinked)}")
    if still_deleted:
        print(f"  Deleted from the backup, still on Proton: {len(still_deleted)}"
              f"   (see {DELETED_LOCALLY})")
    if unrecognized:
        print(f"  Local files matching no Proton photo: {len(unrecognized)}"
              f"   (see {UNRECOGNIZED})")
    print("=" * 66)

    if still_deleted:
        print("")
        print("  Some photos were deleted from the backup but are still on Proton.")
        print("  Deleted on purpose?  ./photo-prune.py "
              + (a.profile if a.profile is not None else a.dest) + " --from-missing")
        print("  Lost by accident?    run again with --redownload-missing")
    protonlib.report_deleted_dir(a.deleted_dir)
    if failed:
        print("")
        print("  Some downloads failed: run again, it retries only those.")
        return 1
    if not cleaned:
        return 1
    print("")
    print("  Backup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
