#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
photo-compare.py - Which local photos are missing from Proton Photos?

WHY
  Typical case: a Dropbox "Camera Uploads" folder, while the same phone also
  synced its photos to Proton Photos. Most photos are on both sides, some
  aren't, and you need to know exactly which ones before the old copy goes
  away. Names don't help: Dropbox renames uploads after their capture time
  ("2024-05-03 10.22.11.jpg") while the phone uploads "IMG_1234.HEIC"; and
  the bytes often differ too (Dropbox can convert HEIC to JPG).

HOW PHOTOS ARE MATCHED (each Proton photo covers at most one local file)
  1. CONTENT: identical sha1. Certain. Needs the file to be on disk.
  2. CAPTURE TIME: same capture second and same kind (photo/video). The local
     capture time comes from the Dropbox-style file name, which works even
     on online-only placeholders, or from EXIF via exiftool for files on
     disk. Times without a time zone are read in --tz (default: this Mac's
     time zone, daylight saving included). Exact seconds are matched first,
     then within --tolerance (default 1 s: the two sides round differently).
     Each Proton photo is used once, so a burst of shots can't make a
     missing photo look present.
  Everything else is MISSING. When in doubt a photo is reported missing:
  re-uploading it costs at worst a duplicate, missing it costs the photo.

  Matching by "any time zone" was tried and rejected: it produced matches at
  offsets like +2h15, i.e. coincidences. Photos taken while travelling in
  another time zone may therefore show up as missing (and be uploaded as
  duplicates): that's the safe direction.

RESULT
  - present: identical content, or same capture time
  - MISSING: no Proton photo matches
  - can't be checked: online-only placeholders whose name holds no date.
    Make the folder available offline and run again.

  Lists are written to --out-dir (default ~/drive-sync-logs). The Proton
  timeline is read with `photo timeline -d -j` and saved there too, as
  proton-photo-timeline.json: --reuse-scan reuses it instead of reading it
  again, like the Drive scan. --inventory FILE reuses another one, e.g. the
  .proton-photo-inventory.json of a photo-backup.py backup.

UPLOAD SCRIPT
  With --upload-script it also writes photos-upload-<label>-<timestamp>.sh,
  which uploads the missing photos to Proton Photos with
  `proton-drive photo upload -c rename` (a name clash with a DIFFERENT photo
  must never cause a missing one to be skipped). Review it, run it, then run
  this comparison again. Online-only files must be made available offline
  first: the script skips files that are empty when it runs.

PROFILES
  The folder and the options can live in a profile, in the same
  configuration file as the other scripts: ~/.config/proton-backup/config.ini
  (another one with --config), section [photo-compare <name>]. Create or
  change one with ./photo-compare.py --setup camera, then run
  ./photo-compare.py camera, or just ./photo-compare.py when it's the only
  photo-compare profile. Folder paths work too, with the defaults.
  Command-line options override the profile.

USAGE
  ./photo-compare.py --setup camera
  ./photo-compare.py camera
  ./photo-compare.py ~/Dropbox/"Camera Uploads"
  ./photo-compare.py ~/Dropbox/"Camera Uploads" ~/Pictures/Phone --upload-script
  ./photo-compare.py camera --reuse-scan

SAFETY
  Read-only. Doesn't upload, modify or delete anything: the upload script is
  only written, never run. Online-only files are never read (that would
  download them).
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone

import protonlib

PHOTO_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff",
             ".webp", ".dng", ".raw", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mkv", ".mts"}
SF_DATALESS = 0x40000000     # macOS flag on online-only files not materialized
JUNK_DIRS = {".dropbox.cache", ".Trashes", ".Trash", ".TemporaryItems",
             ".Spotlight-V100", ".fseventsd", "#recycle"}

# "2024-05-03 10.22.11.jpg", "2024-05-03 10.22.11-1.jpg": Dropbox Camera
# Uploads names files after the local capture time
DROPBOX_NAME = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2}) (\d{2})\.(\d{2})\.(\d{2})(?:-\d+| \(\d+\))?\.\w+$")
EXIF_TIME = re.compile(
    r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?([+-]\d{2}:\d{2}|Z)?$")


SCRIPT = "photo-compare"
HELP = {k: h for k, _, h in protonlib.PROFILE_SETTINGS[SCRIPT]}


def parse_args():
    p = argparse.ArgumentParser(
        description="Which local photos are missing from Proton Photos?",
        epilog="Settings: ~/.config/proton-backup/config.ini (see config.example.ini). "
               "Options given here override the profile.")
    p.add_argument("targets", nargs="*", metavar="target",
                   help="a profile from the configuration file (optional when there's "
                        "only one), or local folders with photos (searched recursively)")
    p.add_argument("--setup", action="store_true",
                   help="create or change the profile named by TARGET, asking questions")
    p.add_argument("--config", default=protonlib.CONFIG_PATH,
                   help="configuration file (default ~/.config/proton-backup/config.ini)")
    g = p.add_argument_group("settings (override the profile)")
    g.add_argument("--tz",
                   help="time zone of capture times without one, e.g. Europe/Rome "
                        "(default: this Mac's time zone)")
    g.add_argument("--tolerance", type=int,
                   help="seconds of tolerance on capture times (default 1, 0 = exact)")
    ex = g.add_mutually_exclusive_group()
    ex.add_argument("--exif", dest="exif", action="store_const", const="yes",
                    help="read EXIF dates with exiftool (the default)")
    ex.add_argument("--no-exif", dest="exif", action="store_const", const="no",
                    help="don't read EXIF dates with exiftool")
    g.add_argument("--upload-script", dest="upload_script", action="store_const", const="yes",
                   help="also write a script that uploads the missing photos to Proton")
    g.add_argument("--batch", type=int,
                   help="photos per upload command in the upload script (default 20)")
    p.add_argument("--reuse-scan", action="store_true",
                   help="reuse the Proton timeline saved last time "
                        "(~/drive-sync-logs/proton-photo-timeline.json) instead of reading it")
    p.add_argument("--inventory", default=None, metavar="FILE",
                   help="reuse another saved Proton timeline (JSON from `photo timeline -d -j`)")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI, help="path to the CLI binary")
    p.add_argument("--exiftool", default="exiftool")
    p.add_argument("--out-dir", default=os.path.expanduser("~/drive-sync-logs"))
    p.add_argument("--label", default=None,
                   help="label used in output file names (default: the profile, or the "
                        "name of the first folder)")
    p.add_argument("--max-print", type=int, default=30)
    a = p.parse_args()
    a.config = os.path.expanduser(a.config)
    a.out_dir = os.path.expanduser(a.out_dir)
    if a.setup and len(a.targets) > 1:
        p.error("--setup takes one profile name")
    return a


def resolve_settings(a):
    """Settings for this run, copied onto `a` (a.folders, a.tz, a.tzinfo,
    a.tolerance, a.no_exif, a.upload_script, a.batch, a.label)."""
    cfg = protonlib.read_config(a.config)
    name = None
    if len(a.targets) <= 1:
        name = protonlib.choose_profile(cfg, SCRIPT, (a.targets or [None])[0], a.config)
    s = protonlib.load_settings(cfg, SCRIPT, name)
    if name is None:
        folders = a.targets
        where = "defaults and command line"
    else:
        where = f"profile [{protonlib.profile_section(SCRIPT, name)}] in {a.config}"
        if not s["folder"]:
            sys.exit(f"ERROR: {where} has no folder. Run: ./{SCRIPT}.py --setup {name}")
        folders = [s["folder"]]
    for k in ("tz", "tolerance", "exif", "upload_script", "batch"):
        v = getattr(a, k)
        if v is not None:
            s[k] = str(v)
    protonlib.check_settings(s, where)

    a.profile, a.where = name, where
    a.folders = [os.path.expanduser(f) for f in folders]
    for d in a.folders:
        if not os.path.isdir(d):
            sys.exit(f"ERROR: {d} doesn't exist ({where})")
    a.tz = s["tz"] or None
    a.tolerance, a.batch = s["tolerance"], s["batch"]
    a.no_exif = s["exif"] == "no"
    a.upload_script = s["upload_script"] == "yes"
    if not a.label:
        a.label = name or os.path.basename(a.folders[0].rstrip(os.sep)) or "photos"
    a.label = re.sub(r"[^A-Za-z0-9._-]+", "_", a.label).strip("_") or "photos"
    a.tzinfo = None
    if a.tz:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            a.tzinfo = ZoneInfo(a.tz)
        except (ZoneInfoNotFoundError, ValueError):
            sys.exit(f"ERROR: unknown time zone {a.tz!r} ({where}), e.g. Europe/Rome")
    return a


def setup_questions(cur):
    ask = protonlib.ask
    new = {}
    print("")
    print(f"  {HELP['folder']}")
    new["folder"] = protonlib.ask_existing_folder(
        "folder", cur["folder"] or "~/Dropbox/Camera Uploads")
    for k in ("tz", "tolerance", "exif", "upload_script", "batch"):
        print("")
        print(f"  {HELP[k]}")
        if k == "tz":
            while True:
                v = ask(k, cur[k] or "this Mac's")
                v = "" if v == "this Mac's" else v
                if not v:
                    break
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(v)
                    break
                except Exception:
                    print("    unknown time zone, e.g. Europe/Rome")
            new[k] = v
            continue
        new[k] = ask(k, cur[k], protonlib.SETTING_CHOICES.get(k),
                     protonlib.SETTING_MINIMUM.get(k))
    return new



def kind_of_ext(ext):
    return "video" if ext.lower() in VIDEO_EXT else "photo"


def to_epoch(naive_or_aware, tzinfo):
    """Epoch seconds. A naive time is read in tzinfo, or in this Mac's time
    zone (with its daylight saving rules) when tzinfo is None."""
    t = naive_or_aware
    if t.tzinfo is None and tzinfo is not None:
        t = t.replace(tzinfo=tzinfo)
    return int(t.timestamp())


# --- Proton side -------------------------------------------------------------

TIMELINE = "proton-photo-timeline.json"


def load_timeline(a):
    saved = os.path.join(a.out_dir, TIMELINE)
    path = os.path.expanduser(a.inventory) if a.inventory else (saved if a.reuse_scan else None)
    if path:
        if not os.path.exists(path):
            sys.exit(f"ERROR: no saved Proton timeline in {path}. Run without "
                     f"{'--inventory' if a.inventory else '--reuse-scan'}.")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        print(f"  Proton timeline reused: {path}")
        return data
    protonlib.suggest_reuse(saved, "The Proton timeline saved last time", "--reuse-scan")
    print("  Reading the Proton Photos timeline...")
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
    os.makedirs(a.out_dir, exist_ok=True)
    protonlib.write_json(saved, data)
    print(f"  {len(data)} photos read in {int(time.time() - t0)}s, saved to {saved}")
    print("  (--reuse-scan reuses it next time)")
    return data


def index_proton(timeline):
    """sha1 -> uids, capture second -> uids, and uid -> (kind, name)."""
    by_sha, by_sec, info = {}, {}, {}
    for x in timeline:
        uid = x.get("uid") or x.get("nodeUid")
        if not uid:
            continue
        name = (x.get("name") or {}).get("value") or ""
        media = x.get("mediaType") or ""
        if media:
            kind = "video" if media.startswith("video") else "photo"
        else:
            kind = kind_of_ext(os.path.splitext(name)[1])
        info[uid] = (kind, name)
        rev = x.get("activeRevision") or {}
        h = (rev.get("claimedDigests") or {}).get("sha1")
        if h:
            by_sha.setdefault(h, []).append(uid)
        cam = (rev.get("claimedAdditionalMetadata") or {}).get("Camera") or {}
        ct = (x.get("photo") or {}).get("captureTime") or cam.get("CaptureTime")
        if ct:
            try:
                sec = int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
                by_sec.setdefault(sec, []).append(uid)
            except ValueError:
                pass
    return by_sha, by_sec, info


# --- local side --------------------------------------------------------------

def scan_local(folders):
    files = []
    for root in folders:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in JUNK_DIRS and not d.startswith(".")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if fn.startswith(".") or (ext not in PHOTO_EXT and ext not in VIDEO_EXT):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not os.path.isfile(full) or os.path.islink(full):
                    continue
                dataless = bool(getattr(st, "st_flags", 0) & SF_DATALESS)
                files.append({
                    "path": full, "name": fn, "kind": kind_of_ext(ext),
                    "size": st.st_size,
                    # a 0-byte photo is an online-only placeholder, not a photo
                    "on_disk": not dataless and st.st_size > 0,
                    "match": None, "proton": None, "delta": None,
                    "time": None, "time_src": None, "sha1": None,
                    "reason": None, "copy_of": None,
                })
    return files


def name_time(name, tzinfo):
    m = DROPBOX_NAME.match(name)
    if not m:
        return None
    try:
        naive = datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None
    return to_epoch(naive, tzinfo)


def exif_times(a, paths):
    """path -> epoch seconds, from DateTimeOriginal (+ OffsetTimeOriginal)
    or the QuickTime CreationDate with its time zone."""
    out = {}
    if not paths:
        return out
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        try:
            r = subprocess.run([a.exiftool, "-j", "-q", "-DateTimeOriginal",
                                "-OffsetTimeOriginal", "-CreationDate"] + chunk,
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            print(f"  WARNING: '{a.exiftool}' not found: EXIF dates not used "
                  f"(brew install exiftool, or --no-exif to silence this).")
            return out
        try:
            rows = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for e in rows:
            t = None
            dto, off, cd = (e.get("DateTimeOriginal"), e.get("OffsetTimeOriginal"),
                            e.get("CreationDate"))
            for value, extra_tz, is_dto in ((dto, off, True), (cd, None, False)):
                m = EXIF_TIME.match(str(value or "").strip())
                if not m:
                    continue
                y, mo, d, h, mi, s, tz = m.groups()
                tz = tz or (str(extra_tz).strip() if extra_tz else None)
                try:
                    naive = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
                except ValueError:
                    continue
                if tz and tz != "Z" and re.match(r"^[+-]\d{2}:\d{2}$", tz):
                    t = int(datetime.strptime(f"{y}-{mo}-{d} {h}:{mi}:{s}{tz.replace(':', '')}",
                                              "%Y-%m-%d %H:%M:%S%z").timestamp())
                elif tz == "Z":
                    t = int(naive.replace(tzinfo=timezone.utc).timestamp())
                elif is_dto:
                    # DateTimeOriginal is local time; a QuickTime date without
                    # a zone is usually UTC, so it's not guessed
                    t = to_epoch(naive, a.tzinfo)
                if t is not None:
                    break
            if t is not None:
                out[e.get("SourceFile")] = t
    return out


# --- matching ----------------------------------------------------------------

def match(a, files, by_sha, by_sec, info):
    used = set()

    # 1. identical content
    on_disk = [f for f in files if f["on_disk"]]
    t0 = time.time()
    for i, f in enumerate(on_disk, 1):
        h = protonlib.sha1_of(f["path"])
        f["sha1"] = h
        uids = by_sha.get(h) if h else None
        if uids:
            free = [u for u in uids if u not in used]
            uid = free[0] if free else uids[0]
            used.add(uid)
            f["match"], f["proton"] = "content", uid
        if i % 100 == 0 or i == len(on_disk):
            sys.stderr.write(f"\r  sha1: {i}/{len(on_disk)} files | {int(time.time() - t0)}s   ")
            sys.stderr.flush()
    if on_disk:
        sys.stderr.write("\n")

    # 2. capture time: from the Dropbox-style name, else from EXIF
    rest = [f for f in files if not f["match"]]
    for f in rest:
        t = name_time(f["name"], a.tzinfo)
        if t is not None:
            f["time"], f["time_src"] = t, "name"
    need_exif = [f["path"] for f in rest if f["time"] is None and f["on_disk"]]
    if need_exif and not a.no_exif:
        print(f"  Reading EXIF dates of {len(need_exif)} files...")
        times = exif_times(a, need_exif)
        for f in rest:
            if f["path"] in times:
                f["time"], f["time_src"] = times[f["path"]], "EXIF"

    # one Proton photo per local file: exact seconds first, then the tolerance
    timed = sorted((f for f in rest if f["time"] is not None),
                   key=lambda f: (f["time"], f["path"]))
    deltas = [0]
    for d in range(1, a.tolerance + 1):
        deltas += [d, -d]
    for delta in deltas:
        for f in timed:
            if f["match"]:
                continue
            for uid in by_sec.get(f["time"] + delta, []):
                if uid not in used and info[uid][0] == f["kind"]:
                    used.add(uid)
                    f["match"], f["proton"], f["delta"] = "time", uid, delta
                    break

    # 3. local copies: files with identical content are the same photo. If
    # one of them is on Proton, so are the others; if none is, only one is
    # reported missing (and uploaded)
    groups = {}
    for f in files:
        if f["sha1"]:
            groups.setdefault(f["sha1"], []).append(f)
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=lambda f: f["path"])
        matched = [f for f in g if f["match"] and f["match"] != "local-copy"]
        keeper = matched[0] if matched else g[0]
        for f in g:
            if f is not keeper and not f["match"]:
                f["match"], f["copy_of"] = "local-copy", keeper["path"]

    # why each remaining file is missing
    for f in files:
        if f["match"]:
            continue
        if f["time"] is None:
            f["reason"] = "no capture date; content not on Proton"
            continue
        near = any(info[u][0] == f["kind"]
                   for d in deltas for u in by_sec.get(f["time"] + d, []))
        f["reason"] = ("the Proton photo at that time matches another local file "
                       "(burst or duplicate? check)" if near
                       else "no Proton photo at that time")


# --- main --------------------------------------------------------------------

def main():
    a = parse_args()
    if a.setup:
        return protonlib.run_setup(a.config, SCRIPT, (a.targets or [None])[0],
                                   setup_questions)
    resolve_settings(a)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    print("")
    for d in a.folders:
        print(f"  LOCAL  : {d}")
    zone = a.tz or "this Mac's time zone"
    print(f"  Times without a zone read in: {zone}")
    print("")

    timeline = load_timeline(a)
    by_sha, by_sec, info = index_proton(timeline)
    if not by_sha:
        print("  WARNING: the timeline has no sha1 (read without -d?): "
              "matching by capture time only.")

    files = scan_local(a.folders)
    print(f"  {len(files)} local photos and videos "
          f"({sum(1 for f in files if not f['on_disk'])} online-only)")
    match(a, files, by_sha, by_sec, info)

    content = [f for f in files if f["match"] == "content"]
    by_time = [f for f in files if f["match"] == "time"]
    copies = [f for f in files if f["match"] == "local-copy"]
    exact = sum(1 for f in by_time if f["delta"] == 0)
    missing = sorted((f for f in files
                      if not f["match"] and (f["on_disk"] or f["time"] is not None)),
                     key=lambda f: f["path"])
    unknown = sorted((f for f in files
                      if not f["match"] and not f["on_disk"] and f["time"] is None),
                     key=lambda f: f["path"])
    missing_offline = sum(1 for f in missing if not f["on_disk"])

    print("")
    print("=" * 66)
    print(f"  Local photos and videos ........ {len(files)}")
    print(f"  On Proton, identical content ... {len(content)}")
    print(f"  On Proton, same capture time ... {len(by_time)}"
          f"   (to the second: {exact}, within ±{a.tolerance}s: {len(by_time) - exact})")
    if copies:
        print(f"  Copies of another local file ... {len(copies)}"
              f"   (identical content, counted once)")
    print(f"  MISSING on Proton .............. {len(missing)}"
          + (f"   (online-only: {missing_offline})" if missing_offline else ""))
    reasons = {}
    for f in missing:
        reasons[f["reason"]] = reasons.get(f["reason"], 0) + 1
    for r, n in sorted(reasons.items(), key=lambda t: -t[1]):
        print(f"     {n:>6}  {r}")
    if unknown:
        print(f"  Can't be checked ............... {len(unknown)}"
              f"   (online-only, no date in the name)")
    print("=" * 66)

    def dump(title, items, fmt):
        if not items:
            return
        print("")
        print(title)
        for f in items[:a.max_print]:
            print("   " + fmt(f))
        if len(items) > a.max_print:
            print(f"   ... and {len(items) - a.max_print} more")

    dump("MISSING ON PROTON:", missing,
         lambda f: f["path"] + ("   [online-only]" if not f["on_disk"] else "")
         + ("   [check: burst or duplicate?]" if "burst" in f["reason"] else ""))
    dump("CAN'T BE CHECKED (make them available offline and run again):", unknown,
         lambda f: f["path"])

    os.makedirs(a.out_dir, exist_ok=True)
    written = []
    for name, rows in (
            ("photos-missing", [f"{f['path']}\t{f['reason']}" for f in missing]),
            ("photos-local-copies", [f"{f['path']}\tsame content as {f['copy_of']}"
                                     for f in copies]),
            ("photos-uncheckable", [f["path"] for f in unknown]),
            ("photos-matched-by-time",
             [f"{f['path']}\t{info[f['proton']][1]}\t{f['time_src']}\t{f['delta']:+d}s"
              for f in by_time])):
        if not rows:
            continue
        p = os.path.join(a.out_dir, f"{name}-{a.label}-{stamp}.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")
        written.append(p)
    if written:
        print("")
        print("Full lists:")
        for p in written:
            print("   " + p)

    if a.upload_script:
        print("")
        if not missing:
            print("  Upload script: nothing to upload.")
        else:
            paths = sorted(f["path"] for f in missing)
            calls = [["My Photos"] + paths[i:i + a.batch]
                     for i in range(0, len(paths), a.batch)]
            folders = (shlex.quote(a.profile) if a.profile
                       else " ".join(shlex.quote(d) for d in a.folders))
            path = os.path.join(a.out_dir, f"photos-upload-{a.label}-{stamp}.sh")
            protonlib.write_upload_script(
                path, a.cli,
                header=[f"Generated by photo-compare.py on {protonlib.generated_on()}.",
                        f"Uploads to Proton Photos the {len(missing)} photos and videos",
                        "found locally but not on Proton, with `photo upload -c rename`."],
                upload_cmd='"$CLI" photo upload -c rename "${files[@]}"',
                calls=calls,
                verify=[f"./photo-compare.py {folders}"])
            print(f"  Upload script: {path}")
            print(f"    {len(missing)} photos and videos to upload.")
            if missing_offline:
                print(f"    {missing_offline} of them are online-only: make the folder")
                print("    available offline first, or they'll be skipped.")
            print("    Review it, run it, then run this comparison again.")

    print("")
    if not missing and not unknown:
        print("  Every local photo is on Proton.")
        return 0
    print("  Some photos are missing on Proton, or couldn't be checked: see above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
