#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
drive-compare.py - Compare a folder with Proton Drive.

WHY
  When migrating to Proton Drive (e.g. from Dropbox), or when a copy of your
  files sits on a disk, you want to know whether anything in that folder is
  missing on Proton. This script scans Proton Drive through the CLI and
  compares it with the folder. Proton exposes the sha1 of each original
  file, so the comparison goes down to the CONTENT without downloading
  anything from Proton: computing the sha1 locally is enough.

  The scan takes seconds even on tens of thousands of files and is saved in
  ~/drive-sync-logs. If it gets interrupted, run the same command again and
  it resumes; --reuse-scan skips it and uses the last complete one.

PROFILES
  The choices (which folder, which side wins, what to show) live in a
  configuration file, ~/.config/proton-backup/config.ini, one profile per
  section. Create or change one with:

      ./drive-compare.py --setup dropbox

  then compare with ./drive-compare.py dropbox. The profile name is used in
  the report and in file names ("Only in dropbox"). A folder path works too
  (./drive-compare.py ~/Dropbox): the defaults apply and the folder is
  called "folder". Command-line options override the profile.
  config.example.ini documents every setting.

WHAT IT DOES WITH EACH FILE IN THE FOLDER
  1. Same path on Proton, same content: identical.
  2. Same path, different content: the "winner" setting decides. proton
     keeps Proton's version (nothing to do); folder copies the folder's
     version over it; newer picks the most recently modified one; ask lists
     it among the files to decide.
  3. Not at the same path: it's looked up in the whole Drive. By sha1 when
     the file is on disk; by name and size, or by name alone, when it's an
     online-only placeholder (0 bytes, no content to hash). If found, it was
     moved or renamed on Proton.
  4. Listed in the ignore file (paths deleted from Proton on purpose): only
     counted.
  5. System files and the content of macOS packages (.pages, .numbers, ...;
     Proton stores a package as a single .pkgf file): only counted.
  6. Anything else is in the folder and nowhere on Proton: a decision is
     needed. If a file or folder with that name is in the Proton trash, the
     report says so.

  Files only on Proton are counted, or listed with proton_only = list.

OUTPUT
  One report in ~/drive-sync-logs, drive-compare-<name>-<timestamp>.txt:
  what needs a decision first, then a count of everything else. --details
  appends the full lists to the same file.

  If at most review_max files (default 20) need a decision and the script
  runs in a terminal, it then asks file by file: copy to Proton, ignore
  forever (added to the ignore file), or skip.

COPY SCRIPT
  When there's something to copy (files where the folder wins, files chosen
  in the review, and with copy_script = yes the files missing on Proton) it
  writes copy-to-proton-<name>-<timestamp>.sh: a bash script that copies
  them into the folder synced by the Proton Drive app, at the same relative
  path, creating subfolders as needed. The app then uploads them.

  The Proton Drive folder is found in ~/Library/CloudStorage: if there's
  only one ProtonDrive folder it's that one; if there are several (e.g. an
  old copy left by a reset, with a date in its name), it's the one linked
  to the app (the only one with the com.apple.file-provider-domain-id
  attribute). With proton_root /my-files/Work the files go in its Work
  subfolder. If it can't be found, set proton_folder (or --proton-folder).
  As a safety check, the script isn't written if none of the scanned
  top-level folders exist in the chosen folder.

  Review the script, then run it. Files that are empty when it runs are
  skipped (online-only placeholders would be copied empty). Missing files
  never overwrite anything; a file where the folder's version wins replaces
  Proton's, which keeps the old one in its version history.

USAGE
  ./drive-compare.py --setup dropbox
  ./drive-compare.py dropbox
  ./drive-compare.py dropbox --details
  ./drive-compare.py ~/Dropbox --exclude "Camera Uploads" --winner ask
  ./drive-compare.py /Volumes/Disk/Work --root /my-files/Work --copy-script

SAFETY
  Read-only towards Proton and the folder. It writes the report, the copy
  script (never run), the configuration (--setup) and the ignore file (only
  when you choose "ignore" in the review). --include-online FORCES the
  download of online-only files to read them.
"""

import argparse
import configparser
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time

import protonlib

TOLERANCE = 2                # seconds of tolerance on modification times
SF_DATALESS = 0x40000000     # macOS flag on "online-only" files not materialized
SHA1_EMPTY = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
MAX_PRINT = 30               # files to decide shown in the terminal

JUNK = (".DS_Store", "desktop.ini", ".localized")
JUNK_DIRS = {".Trashes", ".Trash", ".TemporaryItems", ".Spotlight-V100",
             ".fseventsd", ".DocumentRevisions-V100", "#recycle",
             ".dropbox.cache"}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_DIR = os.path.expanduser("~/.config/proton-backup")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.ini")
NO_PROFILE = "folder"        # name used when a path is given instead of a profile

# order, default and explanation of every profile setting
SETTINGS = [
    ("folder", "",
     "the folder to compare with Proton (e.g. ~/Dropbox, or a copy on a disk)"),
    ("proton_root", "/my-files",
     "the Proton folder it should match (/my-files is the whole Drive)"),
    ("exclude", "",
     "top-level subfolders to skip, comma-separated, exact names"),
    ("compare_by", "content",
     "content = identical files verified by sha1 (slower, certain) | "
     "size = trust name and size"),
    ("winner", "proton",
     "same file, different content: proton = keep Proton's, nothing to do | "
     "folder = copy the folder's over Proton's | newer = the most recently "
     "modified wins | ask = list it among the files to decide"),
    ("moved", "hide",
     "files found on Proton under another path or name: hide = count only | "
     "list = show them in the report"),
    ("proton_only", "hide",
     "files only on Proton: hide = count only | list = show them in the report"),
    ("copy_script", "no",
     "yes = also put the files missing on Proton in the copy script"),
    ("review_max", "20",
     "ask file by file at the end when at most this many need a decision "
     "(0 = never)"),
    ("ignore_file", "",
     "paths deleted from Proton on purpose, one per line, never reported again "
     "(empty = ~/.config/proton-backup/ignore-<profile>.txt)"),
    ("proton_folder", "",
     "the local folder synced by the Proton Drive app matching proton_root "
     "(empty = found automatically)"),
]
DEFAULTS = {k: d for k, d, _ in SETTINGS}
HELP = {k: h for k, _, h in SETTINGS}
CHOICES = {
    "compare_by": ("content", "size"),
    "winner": ("proton", "folder", "newer", "ask"),
    "moved": ("hide", "list"),
    "proton_only": ("hide", "list"),
    "copy_script": ("yes", "no"),
}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def read_config(path):
    cfg = configparser.ConfigParser(interpolation=None,
                                    inline_comment_prefixes=(";", "#"))
    if os.path.exists(path):
        try:
            cfg.read(path, encoding="utf-8")
        except configparser.Error as e:
            sys.exit(f"ERROR: can't read {path}:\n  {e}")
    return cfg


def render_config(cfg):
    """The configuration as text, every setting preceded by its explanation."""
    out = ["# drive-compare.py profiles: one section per folder to compare.",
           "# Run:   ./drive-compare.py <profile>",
           "# Edit:  ./drive-compare.py --setup <profile>, or by hand.",
           "# Settings in [DEFAULT] apply to every profile.", ""]

    def block(items):
        known = [k for k, _, _ in SETTINGS]
        for k in known + sorted(set(items) - set(known)):
            if k not in items:
                continue
            if k in HELP:
                out.append(f"; {HELP[k]}")
            out.append(f"{k} = {items[k]}")
        out.append("")

    if cfg.defaults():
        out.append("[DEFAULT]")
        block(dict(cfg.defaults()))
    for name in cfg.sections():
        out.append(f"[{name}]")
        # only the profile's own keys, not those inherited from [DEFAULT]
        own = {k: v for k, v in cfg.items(name, raw=True)
               if k not in cfg.defaults() or cfg.defaults()[k] != v}
        block(own)
    return "\n".join(out)


def write_config(path, cfg):
    # a symlinked config (e.g. kept in a private repo) stays a symlink
    path = os.path.realpath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render_config(cfg))
    os.replace(tmp, path)


def check_settings(s, where):
    for k, allowed in CHOICES.items():
        if s[k] not in allowed:
            sys.exit(f"ERROR: {where}: {k} = {s[k]!r}, expected one of: {', '.join(allowed)}")
    try:
        s["review_max"] = int(s["review_max"])
        if s["review_max"] < 0:
            raise ValueError
    except ValueError:
        sys.exit(f"ERROR: {where}: review_max must be a number, 0 or more")


def resolve_settings(a):
    """Settings for this run: profile (or defaults), then command-line options."""
    cfg = read_config(a.config)
    target = a.target
    path_like = "/" in target or target.startswith(("~", "."))
    if not path_like and cfg.has_section(target):
        name = target
        s = dict(DEFAULTS)
        s.update({k: v for k, v in cfg.items(name)})
        where = f"profile [{name}] in {a.config}"
        if not s["folder"]:
            sys.exit(f"ERROR: {where} has no folder. Run: ./drive-compare.py --setup {name}")
    elif os.path.isdir(os.path.expanduser(target)):
        name = NO_PROFILE
        s = dict(DEFAULTS)
        s.update(cfg.defaults())
        s["folder"] = target
        where = "defaults and command line"
    elif not path_like:
        profiles = ", ".join(cfg.sections()) or "none yet"
        sys.exit(f"ERROR: '{target}' is neither a profile in {a.config} "
                 f"(profiles: {profiles}) nor a folder.\n"
                 f"  Create the profile with: ./drive-compare.py --setup {target}")
    else:
        sys.exit(f"ERROR: {target} doesn't exist")

    overrides = {"proton_root": a.root, "exclude": a.exclude,
                 "compare_by": a.compare_by, "winner": a.winner,
                 "moved": a.moved, "proton_only": a.proton_only,
                 "ignore_file": a.ignore_file, "proton_folder": a.proton_folder,
                 "review_max": a.review_max}
    if a.copy_script:
        overrides["copy_script"] = "yes"
    if a.sha1:
        overrides["compare_by"] = "content"
    for k, v in overrides.items():
        if v is not None:
            s[k] = str(v)
    check_settings(s, where)

    s["name"] = name
    s["file_name"] = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or NO_PROFILE
    s["folder"] = os.path.expanduser(s["folder"]).rstrip(os.sep) or os.sep
    s["exclude"] = {protonlib.nfc(x.strip()) for x in s["exclude"].split(",") if x.strip()}
    s["copy_script"] = s["copy_script"] == "yes"
    s["ignore_file"] = os.path.expanduser(
        s["ignore_file"] or os.path.join(CONFIG_DIR, f"ignore-{s['file_name']}.txt"))
    s["proton_folder"] = os.path.expanduser(s["proton_folder"]) if s["proton_folder"] else ""
    s["where"] = where
    if not os.path.isdir(s["folder"]):
        sys.exit(f"ERROR: {s['folder']} doesn't exist ({where})")
    return s


def ask(question, default, choices=None):
    hint = f" [{'/'.join(choices)}]" if choices else ""
    while True:
        try:
            ans = input(f"  {question}{hint} ({default}): ").strip()
        except EOFError:
            print("")
            sys.exit("Setup interrupted: nothing written.")
        ans = ans or default
        if choices and ans not in choices:
            print(f"    answer one of: {', '.join(choices)}")
            continue
        return ans


def setup(a):
    """Create or change a profile by asking one question at a time."""
    if not sys.stdin.isatty():
        sys.exit("ERROR: --setup asks questions: run it in a terminal.")
    cfg = read_config(a.config)
    print("")
    print(f"drive-compare setup  (configuration: {a.config})")
    if cfg.sections():
        print(f"  existing profiles: {', '.join(cfg.sections())}")
    print("  Press Enter to keep the value in brackets. Ctrl-C to quit without saving.")
    print("")
    try:
        name = a.target
        while not name or not NAME_RE.match(name):
            if name:
                print("    letters, digits, '.', '_' or '-' only, e.g. dropbox or ssd")
            name = input("  Profile name, e.g. dropbox or ssd: ").strip()
        cur = dict(DEFAULTS)
        cur.update(cfg.defaults())
        if cfg.has_section(name):
            cur.update(cfg.items(name, raw=True))
            print(f"  Changing profile [{name}].")
        else:
            print(f"  New profile [{name}].")
        print("")

        new = {}
        print(f"  {HELP['folder']}")
        while True:
            folder = ask("folder", cur["folder"] or "~/Dropbox")
            if os.path.isdir(os.path.expanduser(folder)):
                break
            print(f"    {folder} doesn't exist (an external disk not plugged in?)")
            if ask("keep it anyway?", "no", ("yes", "no")) == "yes":
                break
        new["folder"] = folder
        for k in ("proton_root", "exclude", "compare_by", "winner", "moved",
                  "proton_only", "copy_script", "review_max"):
            print("")
            print(f"  {HELP[k]}")
            if k == "exclude":
                full = os.path.expanduser(folder)
                if os.path.isdir(full):
                    tops = sorted(d for d in os.listdir(full)
                                  if os.path.isdir(os.path.join(full, d))
                                  and not d.startswith("."))
                    if tops:
                        print(f"    folders in it: {', '.join(tops)}")
                new[k] = ask(k, cur[k] or "none")
                if new[k] == "none":
                    new[k] = ""
                continue
            while True:
                new[k] = ask(k, cur[k], CHOICES.get(k))
                if k != "review_max" or new[k].isdigit():
                    break
                print("    a number, 0 or more")
    except (KeyboardInterrupt, EOFError):
        print("")
        sys.exit("Setup interrupted: nothing written.")

    if not cfg.has_section(name):
        cfg.add_section(name)
    for k, v in new.items():
        cfg.set(name, k, v)
    write_config(a.config, cfg)
    print("")
    print(f"  Saved profile [{name}] in {a.config}")
    print(f"  Compare with:  ./drive-compare.py {name}")
    return 0


# ---------------------------------------------------------------------------
# Reading both sides
# ---------------------------------------------------------------------------

# Structural noise: content of macOS packages, service files. Counted, never
# listed among the files to decide.
PKG_RE = re.compile(r'\.(lh3d|pages|numbers|key|band|rtfd|photoslibrary)/')
ICON_RE = re.compile(r'(^|/)Icon\r?$|(^|/)Icon\.jpg$')
DBX_RE = re.compile(r'^\.dropbox|\.dbx-(backup|passwords)$|(^|/)\.dropbox\.')
LOCK_RE = re.compile(r'(^|/)~\$')
PKGF_RE = re.compile(r'\.pkgf$')


def noise_folder(rel):
    if PKG_RE.search(rel):
        return "inside macOS packages (.pages, .numbers, ...)"
    if ICON_RE.search(rel):
        return "macOS Icon files"
    if DBX_RE.search(rel):
        return "Dropbox service files"
    if LOCK_RE.search(rel):
        return "Office lock files (~$)"
    return None


def noise_proton(rel):
    if PKGF_RE.search(rel):
        return "macOS packages (.pkgf)"
    if LOCK_RE.search(rel):
        return "Office lock files (~$)"
    if ICON_RE.search(rel):
        return "macOS Icon files"
    return None


def scan_folder(root, exclude):
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
            dirnames[:] = [d for d in dirnames if protonlib.nfc(d) not in exclude]
        dirnames[:] = [d for d in dirnames if d not in JUNK_DIRS]
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
                dataless = None   # suspicious: 0 bytes and 0 blocks, confirm with Proton
            files[protonlib.nfc(rel)] = (st.st_size, int(st.st_mtime), full, dataless)
        sys.stderr.write(f"\r  folder: {len(files)} files...")
    sys.stderr.write("\r" + " " * 40 + "\r")
    return files


def get_scan(s, a):
    """Path of an up-to-date, complete scan of the Proton root."""
    tsv = protonlib.scan_path(s["proton_root"])
    if a.reuse_scan:
        if not os.path.exists(tsv) or os.path.exists(tsv + ".state"):
            sys.exit(f"ERROR: no complete scan of {s['proton_root']} to reuse. "
                     "Run without --reuse-scan.")
        print(f"  Scan reused: {tsv}")
        return tsv
    tsv, errors = protonlib.scan_drive(a.cli, s["proton_root"], workers=a.workers)
    if errors:
        protonlib.scan_errors_exit(errors)
    return tsv


def load_scan(path, exclude):
    remote, problems = {}, []
    for rel, size, mtime, sha1, flag in protonlib.read_scan(path):
        rel = protonlib.nfc(rel)
        if rel.split("/", 1)[0] in exclude:
            continue
        if flag != "-":
            problems.append((rel, flag))
        remote[rel] = (size, mtime, sha1)
    return remote, problems


def load_ignore(path):
    try:
        with open(path, encoding="utf-8") as f:
            lines = [protonlib.nfc(x.strip()) for x in f]
    except OSError:
        return []
    return [x.rstrip("/").casefold() for x in lines if x and not x.startswith("#")]


def is_ignored(rel, patterns):
    """A pattern matches the path itself, or everything under it as a folder.
    Wildcards (* ? [..]) as in the shell; * also matches across folders."""
    r = rel.casefold()
    for p in patterns:
        if r == p or r.startswith(p + "/") or fnmatch.fnmatchcase(r, p) \
                or fnmatch.fnmatchcase(r, p + "/*"):
            return True
    return False


def read_trash(cli):
    """Files and folders in the Proton trash: ({sha1: date}, {(type, name): date}).
    Only the top level can be read: the CLI doesn't list inside a trashed
    folder. None if the trash can't be read."""
    try:
        r = protonlib.run_cli(cli, ["filesystem", "list", "/trash", "-j"], timeout=120)
        items = json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None
    if not isinstance(items, list):
        return None
    by_sha, by_name = {}, {}
    for e in items:
        name = (e.get("name") or {}).get("value")
        when = (e.get("trashTime") or "")[:10]
        if name:
            by_name[(e.get("type"), protonlib.nfc(name).casefold())] = when
        sha = ((e.get("activeRevision") or {}).get("claimedDigests") or {}).get("sha1")
        if sha:
            by_sha[sha] = when
    return by_sha, by_name


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

class Result:
    def __init__(self):
        self.identical = []
        self.online = []          # same path, online-only here: not verified
        self.no_hash = []         # same path, no sha1 to compare with
        self.proton_kept = []     # different version, Proton's wins
        self.replace = []         # different version, the folder's wins: copy over
        self.decide_diff = []     # (rel, reason)
        self.moved = []           # (rel, proton path, how)
        self.ignored = []
        self.noise = {}           # category -> [rel]
        self.decide_missing = []  # rel
        self.copy_new = []        # missing files going in the copy script
        self.proton_only = []
        self.proton_noise = {}
        self.problems = []
        self.hints = {}           # rel -> trash hint
        self.local_sha = {}       # rel -> sha1, for the files hashed
        self.hashed = 0


def compare(s, local, remote, include_online):
    res = Result()
    by_content = s["compare_by"] == "content"
    patterns = load_ignore(s["ignore_file"])

    def sha(rel):
        if rel not in res.local_sha:
            res.local_sha[rel] = protonlib.sha1_of(local[rel][2])
            res.hashed += 1
            if res.hashed % 50 == 0:
                sys.stderr.write(f"\r  sha1: {res.hashed} files...")
        return res.local_sha[rel]

    def to_decide_diff(rel, reason):
        if is_ignored(rel, patterns):
            res.ignored.append(rel)
        else:
            res.decide_diff.append((rel, reason))

    # 1-2: same path on both sides
    for rel in sorted(set(local) & set(remote)):
        ls, lm, _, ldataless = local[rel]
        rs, rm, rsha = remote[rel]
        # not physically here: "0 bytes here, content on Proton" is a
        # placeholder, not a divergence
        placeholder = (ldataless is True) or (
            ldataless is None and ls == 0 and rs > 0 and rsha != SHA1_EMPTY)
        if placeholder and not include_online:
            res.online.append(rel)
            continue
        if ls == rs:
            if not by_content:
                res.identical.append(rel)
                continue
            if rsha == "-":
                res.no_hash.append(rel)
                continue
            h = sha(rel)
            if h is None:
                res.no_hash.append(rel)
                continue
            if h == rsha:
                res.identical.append(rel)
                continue
        # different version
        w = s["winner"]
        if w == "newer":
            if lm > rm + TOLERANCE:
                w = "folder"
            elif rm > lm + TOLERANCE:
                w = "proton"
            else:
                to_decide_diff(rel, "same date, different content")
                continue
        if w == "proton":
            res.proton_kept.append(rel)
        elif w == "folder":
            res.replace.append(rel)
        else:
            to_decide_diff(rel, "different content")

    # 3-6: not at the same path on Proton
    by_sha, by_name, by_name_size = {}, {}, {}
    for rel, (size, _, rsha) in remote.items():
        if rsha != "-":
            by_sha.setdefault(rsha, rel)
        key = os.path.basename(rel).casefold()
        by_name.setdefault(key, rel)
        by_name_size.setdefault((key, size), rel)
    for rel in sorted(set(local) - set(remote)):
        cat = noise_folder(rel)
        if cat:
            res.noise.setdefault(cat, []).append(rel)
            continue
        if is_ignored(rel, patterns):
            res.ignored.append(rel)
            continue
        size, _, _, dataless = local[rel]
        key = os.path.basename(rel).casefold()
        has_content = size > 0 and (dataless is not True or include_online)
        if has_content and by_content:
            found, how = by_sha.get(sha(rel)), "same content"
        elif size > 0:
            found, how = by_name_size.get((key, size)), "same name and size"
        else:
            # an online-only placeholder: the name is all there is
            found, how = by_name.get(key), "same name, content not verified"
        if found:
            res.moved.append((rel, found, how))
        else:
            res.decide_missing.append(rel)
    if res.hashed >= 50:
        sys.stderr.write("\r" + " " * 40 + "\r")

    moved_to = {m[1] for m in res.moved}
    for rel in sorted(set(remote) - set(local)):
        cat = noise_proton(rel)
        if cat:
            res.proton_noise.setdefault(cat, []).append(rel)
        elif rel not in moved_to:
            res.proton_only.append(rel)
    return res


def add_trash_hints(res, cli):
    """Note which files to decide have a namesake in the Proton trash.
    False if the trash couldn't be read."""
    if not res.decide_missing:
        return True
    trash = read_trash(cli)
    if trash is None:
        return False
    by_sha, by_name = trash
    for rel in res.decide_missing:
        h = res.local_sha.get(rel)
        parts = rel.casefold().split("/")
        if h and h in by_sha:
            res.hints[rel] = f"in the Proton trash since {by_sha[h]}"
        elif ("file", parts[-1]) in by_name:
            res.hints[rel] = (f"a file with this name is in the Proton trash since "
                              f"{by_name[('file', parts[-1])]}")
        else:
            for i, d in enumerate(rel.split("/")[:-1]):
                when = by_name.get(("folder", parts[i]))
                if when:
                    res.hints[rel] = f"folder '{d}' is in the Proton trash since {when}"
                    break
    return True


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------

def review(s, res, local, remote):
    """Ask what to do with each file to decide. Returns the paths to add to
    the ignore file."""
    items = [("missing", rel, "") for rel in res.decide_missing] + \
            [("diff", rel, why) for rel, why in res.decide_diff]
    name = s["name"]
    print("")
    print(f"{len(items)} files need a decision. For each one:")
    print("  c = copy to Proton   i = ignore forever (deleted from Proton on purpose)")
    print("  s = skip, decide later   q = stop asking")
    to_ignore, copy_new, replace = [], [], []
    for n, (kind, rel, why) in enumerate(items, 1):
        size, mtime = local[rel][0], local[rel][1]
        print("")
        print(f"  [{n}/{len(items)}] {rel}")
        if kind == "missing":
            print(f"      only in {name}: {describe(size, mtime)}")
            if rel in res.hints:
                print(f"      {res.hints[rel]}")
        else:
            rs, rm, _ = remote[rel]
            print(f"      {why}: {name} {describe(size, mtime)}  /  Proton {describe(rs, rm)}")
        while True:
            try:
                ans = input("      c / i / s / q ? ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("")
                ans = "q"
            if ans in ("c", "i", "s", "q"):
                break
        if ans == "q":
            print("  Stopped: the remaining files stay in the report.")
            break
        if ans == "c":
            (copy_new if kind == "missing" else replace).append(rel)
        elif ans == "i":
            to_ignore.append(rel)
    res.decide_missing = [r for r in res.decide_missing
                          if r not in to_ignore and r not in copy_new]
    res.decide_diff = [(r, w) for r, w in res.decide_diff
                       if r not in to_ignore and r not in replace]
    res.ignored.extend(to_ignore)
    res.copy_new.extend(copy_new)
    res.replace.extend(replace)
    return to_ignore


def append_ignore(path, rels):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("# Paths deleted from Proton on purpose: drive-compare.py never\n"
                    "# reports them again. One per line; a folder covers everything\n"
                    "# under it; * and ? work as in the shell.\n")
        f.write(f"\n# added by the review on {protonlib.generated_on()}\n")
        for rel in rels:
            f.write(rel + "\n")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def describe(size, mtime):
    return f"{protonlib.human(size)}, {time.strftime('%Y-%m-%d', time.localtime(mtime))}"


def count_line(label, n):
    return f"  {label} {'.' * max(2, 50 - len(label))} {n:>7,}"


def render_report(s, res, local, remote, stamp, copy_path, details, limit=None):
    name = s["name"]
    out = []
    rules = [f"compare by {s['compare_by']}",
             {"proton": "Proton wins", "folder": f"{name} wins",
              "newer": "the newer file wins", "ask": "ask on different versions"}[s["winner"]],
             f"moved files {'listed' if s['moved'] == 'list' else 'hidden'}",
             f"Proton-only {'listed' if s['proton_only'] == 'list' else 'hidden'}"]
    out.append(f"drive-compare: {name} vs Proton, "
               f"{time.strftime('%d %b %Y %H:%M', time.strptime(stamp, '%Y%m%d-%H%M%S'))}")
    excl = f"   (excluded: {', '.join(sorted(s['exclude']))})" if s["exclude"] else ""
    out.append(f"  {name:<8}: {s['folder']}{excl}")
    out.append(f"  {'Proton':<8}: {s['proton_root']}")
    out.append(f"  {'Settings':<8}: {s['where']}")
    out.append(f"  {'Rules':<8}: {' · '.join(rules)}")
    out.append(f"  {'Ignore':<8}: {s['ignore_file']}"
               + ("" if os.path.exists(s["ignore_file"]) else " (not created yet)"))

    def listing(items, fmt):
        shown = items if limit is None else items[:limit]
        for it in shown:
            out.append("    " + fmt(it))
        if len(shown) < len(items):
            out.append(f"    ... and {len(items) - len(shown)} more in the report file")

    n_decide = len(res.decide_missing) + len(res.decide_diff)
    out.append("")
    out.append(f"NEEDS A DECISION: {n_decide}")
    if not n_decide:
        out.append(f"  (nothing in {name} is missing from Proton)")
    if res.decide_missing:
        out.append(f"  Only in {name}, not found anywhere on Proton"
                   + (" (in the copy script):" if s["copy_script"] else ":"))
        listing(res.decide_missing,
                lambda rel: f"{rel}   {describe(*local[rel][:2])}"
                            + (f"   [{res.hints[rel]}]" if rel in res.hints else ""))
    if res.decide_diff:
        out.append(f"  Different content in {name} and on Proton:")
        listing(res.decide_diff,
                lambda t: f"{t[0]}   {name} {describe(*local[t[0]][:2])} / "
                          f"Proton {describe(*remote[t[0]][:2])}"
                          + ("" if t[1] == "different content" else f"   [{t[1]}]"))
    if res.problems:
        out.append("")
        out.append(f"PROBLEMS ON PROTON: {len(res.problems)} files with an unreadable name or size")
        listing(res.problems, lambda t: f"{t[0]}  [{t[1]}]")

    if copy_path:
        out.append("")
        out.append(f"COPY SCRIPT: {copy_path}")
        if res.copy_new:
            out.append(f"  {len(res.copy_new)} files missing on Proton")
        if res.replace:
            out.append(f"  {len(res.replace)} files replacing Proton's version (kept in its history)")
        out.append("  Review it, run it, wait for the Proton Drive app to sync, then compare again.")

    if s["moved"] == "list" and res.moved:
        out.append("")
        out.append(f"MOVED OR RENAMED ON PROTON: {len(res.moved)}")
        listing(res.moved, lambda t: f"{t[0]}  ->  {t[1]}   [{t[2]}]")
    if s["proton_only"] == "list" and res.proton_only:
        out.append("")
        out.append(f"ONLY ON PROTON: {len(res.proton_only)}")
        listing(res.proton_only, lambda rel: rel)

    out.append("")
    out.append("NO ACTION NEEDED")
    by_name_only = sum(1 for m in res.moved if m[2].startswith("same name,"))
    lines = [
        (f"identical ({'content verified' if s['compare_by'] == 'content' else 'name and size'})",
         len(res.identical)),
        ("moved or renamed on Proton", len(res.moved)),
        ("  of which matched by name only", by_name_only),
        ("different version, Proton's kept", len(res.proton_kept)),
        (f"different version, {name}'s copied over", len(res.replace)),
        ("deleted on purpose (ignore file)", len(res.ignored)),
        ("only on Proton", len(res.proton_only)),
        (f"online-only in {name}, not verified", len(res.online)),
        ("same size, no hash to compare", len(res.no_hash)),
    ]
    for cat, items in sorted(res.noise.items()):
        lines.append((cat, len(items)))
    for cat, items in sorted(res.proton_noise.items()):
        lines.append((f"{cat}, on Proton", len(items)))
    for label, n in lines:
        if n:
            out.append(count_line(label, n))
    if res.online:
        out.append("")
        out.append(f"  Online-only files exist on both sides but have no content in {name}:")
        out.append("  make the folder available offline, or use --include-online")
        out.append("  (which downloads them), to verify them too.")

    if details:
        sections = [
            ("Moved or renamed on Proton", [f"{a} -> {b}   [{c}]" for a, b, c in res.moved]),
            ("Different version, Proton's kept", res.proton_kept),
            (f"Different version, {name}'s copied over", res.replace),
            ("Deleted on purpose (ignore file)", res.ignored),
            ("Only on Proton", res.proton_only),
            (f"Online-only in {name}, not verified", res.online),
            ("Same size, no hash to compare", res.no_hash),
        ] + [(f"{c}, only in {name}", v) for c, v in sorted(res.noise.items())] \
          + [(f"{c}, on Proton", v) for c, v in sorted(res.proton_noise.items())]
        out.append("")
        out.append("=" * 66)
        out.append("DETAILS")
        for title, items in sections:
            if items:
                out.append("")
                out.append(f"{title}: {len(items)}")
                out.extend("    " + x for x in items)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Copy script
# ---------------------------------------------------------------------------

COPY_SCRIPT = """#!/bin/bash
{header}
#
# Review this file before running it: nothing has been copied yet.
# Files missing on Proton never overwrite anything. Files where the
# folder's version wins replace Proton's version, which Proton keeps in the
# file's version history. Files that are empty at run time are skipped: an
# online-only placeholder (0 bytes) would otherwise be copied as an empty
# file. Make the source folders available offline first.

DEST={dest}
copied=0; replaced=0; skipped=0; existing=0; failed=0

if [ ! -d "$DEST" ]; then
    echo "Proton Drive folder not found: $DEST"
    echo "Check the path (ls ~/Library/CloudStorage/) and generate the script again."
    exit 1
fi

# copy_file SOURCE DESTINATION: a file missing on Proton, never overwrites
copy_file() {{
    if [ ! -s "$1" ]; then
        echo "  skipped, empty or missing (online-only?): $1"
        skipped=$((skipped + 1))
    elif [ -e "$2" ]; then
        echo "  skipped, already in the Proton folder: $2"
        existing=$((existing + 1))
    elif mkdir -p "$(dirname "$2")" && cp -p "$1" "$2"; then
        copied=$((copied + 1))
        [ $((copied % 50)) -eq 0 ] && echo "  $copied copied..."
    else
        echo "  FAILED: $1"
        failed=$((failed + 1))
    fi
}}

# replace_file SOURCE DESTINATION: the folder's version replaces Proton's
replace_file() {{
    if [ ! -s "$1" ]; then
        echo "  skipped, empty or missing (online-only?): $1"
        skipped=$((skipped + 1))
    elif mkdir -p "$(dirname "$2")" && cp -p "$1" "$2"; then
        replaced=$((replaced + 1))
    else
        echo "  FAILED: $1"
        failed=$((failed + 1))
    fi
}}

{calls}

echo ""
echo "Copied: $copied   Replaced: $replaced   Skipped: $skipped   Already there: $existing   Failed: $failed"
echo ""
echo "The Proton Drive app uploads the copied files in the background."
echo "When it has finished syncing, verify by comparing again:"
echo {verify}
[ "$failed" -eq 0 ]
"""


CLOUD_STORAGE = os.path.expanduser("~/Library/CloudStorage")
DOMAIN_ATTR = "com.apple.file-provider-domain-id"


def linked_to_app(path):
    """True if the folder is the root of an active File Provider domain."""
    try:
        r = subprocess.run(["xattr", "-p", DOMAIN_ATTR, path],
                           capture_output=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return False
    return r.returncode == 0


def find_proton_folder():
    """The folder synced by the Proton Drive app: (path, None) or (None, error)."""
    try:
        names = sorted(n for n in os.listdir(CLOUD_STORAGE)
                       if n.startswith("ProtonDrive")
                       and os.path.isdir(os.path.join(CLOUD_STORAGE, n)))
    except OSError:
        names = []
    paths = [os.path.join(CLOUD_STORAGE, n) for n in names]
    if len(paths) == 1:
        return paths[0], None
    if not paths:
        return None, (f"no Proton Drive folder in {CLOUD_STORAGE}. Is the Proton Drive "
                      "app installed and signed in? Otherwise set proton_folder.")
    linked = [p for p in paths if linked_to_app(p)]
    if len(linked) == 1:
        return linked[0], None
    return None, ("several Proton Drive folders, and it's not clear which one the app "
                  "uses:\n" + "\n".join("    " + p for p in paths)
                  + "\n  Set the right one with proton_folder (or --proton-folder).")


def write_copy_script(s, res, local, remote, path):
    """Write the copy script; return its path, or None if the folder is unusable."""
    if s["proton_folder"]:
        dest = s["proton_folder"].rstrip(os.sep)
    else:
        base, err = find_proton_folder()
        if not base:
            print(f"  ERROR: {err}")
            return None
        print(f"  Proton Drive folder (found automatically): {base}")
        # the synced folder is /my-files: a deeper root is a subfolder of it
        root = "/" + s["proton_root"].strip("/")
        if root == "/my-files":
            dest = base
        elif root.startswith("/my-files/"):
            dest = os.path.join(base, root[len("/my-files/"):])
        else:
            print(f"  ERROR: {s['proton_root']} isn't under /my-files: set the matching "
                  "local folder with proton_folder.")
            return None
    if not os.path.isdir(dest):
        print(f"  ERROR: Proton folder not found: {dest}")
        print("  Find it with: ls ~/Library/CloudStorage/ | grep -i proton")
        return None
    # the folder must match the scanned path: some of the scanned top-level
    # names must be there (a subfolder scan pointed at the root fails here)
    tops = {rel.split("/", 1)[0] for rel in remote}
    if tops and not any(os.path.exists(os.path.join(dest, t)) for t in tops):
        print(f"  ERROR: none of the scanned top-level items is in {dest}.")
        print("  Check proton_root, or set the local folder matching it with proton_folder.")
        return None
    folder = s["folder"]
    real_dest, real_src = os.path.realpath(dest), os.path.realpath(folder)
    if (real_dest + os.sep).startswith(real_src + os.sep) or \
            (real_src + os.sep).startswith(real_dest + os.sep):
        print(f"  ERROR: the Proton folder and {folder} overlap.")
        return None

    calls = []
    for fn, rels in (("copy_file", res.copy_new), ("replace_file", res.replace)):
        for rel in sorted(rels):
            src = local[rel][2]
            dst = os.path.join(dest, os.path.relpath(src, folder))
            calls.append(f"{fn} {shlex.quote(src)} {shlex.quote(dst)}")
    header = [f"Generated by drive-compare.py on {protonlib.generated_on()}.",
              f"Copies into the Proton Drive folder, from {s['name']}:",
              f"  {folder}",
              f"  {len(res.copy_new)} files missing on Proton (copy_file)",
              f"  {len(res.replace)} files replacing Proton's version (replace_file)",
              "keeping the same relative paths under",
              f"  {dest}",
              "The Proton Drive app, running in the background, then uploads them."]
    if s["name"] != NO_PROFILE:
        again = f"    ./drive-compare.py {shlex.quote(s['name'])}"
    else:
        again = (f"    ./drive-compare.py {shlex.quote(folder)} "
                 f"--root {shlex.quote(s['proton_root'])}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(COPY_SCRIPT.format(
            header="\n".join("# " + h for h in header),
            dest=shlex.quote(dest), calls="\n".join(calls), verify=shlex.quote(again)))
    os.chmod(path, 0o755)
    return path


# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Compare a folder with Proton Drive.",
        epilog="Settings: ~/.config/proton-backup/config.ini (see config.example.ini). "
               "Options given here override the profile.")
    p.add_argument("target", nargs="?",
                   help="a profile from the configuration file, or a folder path")
    p.add_argument("--setup", action="store_true",
                   help="create or change the profile named by TARGET, asking questions")
    p.add_argument("--config", default=CONFIG_PATH,
                   help="configuration file (default ~/.config/proton-backup/config.ini)")
    g = p.add_argument_group("settings (override the profile)")
    g.add_argument("--root", help="Proton folder to compare with (default /my-files)")
    g.add_argument("--exclude", help="top-level subfolders to skip, comma-separated")
    g.add_argument("--compare-by", choices=CHOICES["compare_by"])
    g.add_argument("--winner", choices=CHOICES["winner"],
                   help="who wins when a file has different content on the two sides")
    g.add_argument("--moved", choices=CHOICES["moved"])
    g.add_argument("--proton-only", choices=CHOICES["proton_only"])
    g.add_argument("--copy-script", action="store_true",
                   help="put the files missing on Proton in the copy script")
    g.add_argument("--review-max", type=int,
                   help="interactive review up to this many files (0 = never)")
    g.add_argument("--ignore-file")
    g.add_argument("--proton-folder",
                   help="local folder synced by the Proton Drive app, matching --root")
    p.add_argument("--details", action="store_true",
                   help="append the full lists to the report")
    p.add_argument("--no-review", action="store_true", help="never ask, only write the report")
    p.add_argument("--include-online", action="store_true",
                   help="also check online-only files: FORCES THEIR DOWNLOAD")
    p.add_argument("--reuse-scan", action="store_true",
                   help="reuse the last complete scan instead of scanning Proton again")
    p.add_argument("--workers", type=int, default=3,
                   help="parallel calls while scanning (default 3)")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI, help="path to the CLI binary")
    p.add_argument("--out-dir", default=protonlib.LOG_DIR)
    # old names, still accepted
    p.add_argument("--sha1", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--exclude-dirs", dest="exclude", help=argparse.SUPPRESS)
    a = p.parse_args()
    a.config = os.path.expanduser(a.config)
    a.out_dir = os.path.expanduser(a.out_dir)
    if not a.setup and not a.target:
        p.error("name a profile or a folder (or create a profile with --setup NAME)")
    return a


def main():
    a = parse_args()
    if a.setup:
        return setup(a)
    s = resolve_settings(a)

    print("")
    tsv = get_scan(s, a)
    print("")
    print("Reading...")
    remote, problems = load_scan(tsv, s["exclude"])
    local = scan_folder(s["folder"], s["exclude"])
    t0 = time.time()
    res = compare(s, local, remote, a.include_online)
    res.problems = problems
    if res.hashed:
        print(f"  {res.hashed} files verified by content in {int(time.time() - t0)}s")
    if not add_trash_hints(res, a.cli):
        print("  (the Proton trash couldn't be read: no trash hints)")

    n_decide = len(res.decide_missing) + len(res.decide_diff)
    to_ignore = []
    if n_decide and not a.no_review and n_decide <= s["review_max"] and sys.stdin.isatty():
        to_ignore = review(s, res, local, remote)
        if to_ignore:
            append_ignore(s["ignore_file"], to_ignore)
    if s["copy_script"]:
        res.copy_new.extend(r for r in res.decide_missing if r not in res.copy_new)

    os.makedirs(a.out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copy_path = None
    if res.copy_new or res.replace:
        print("")
        copy_path = write_copy_script(
            s, res, local, remote,
            os.path.join(a.out_dir, f"copy-to-proton-{s['file_name']}-{stamp}.sh"))
    report = os.path.join(a.out_dir, f"drive-compare-{s['file_name']}-{stamp}.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write(render_report(s, res, local, remote, stamp, copy_path, a.details))

    print("")
    print("=" * 66)
    print(render_report(s, res, local, remote, stamp, copy_path, False, limit=MAX_PRINT), end="")
    print("=" * 66)
    if to_ignore:
        print(f"  {len(to_ignore)} paths added to {s['ignore_file']}")
    print(f"  Report: {report}")
    print("")
    if len(res.decide_missing) + len(res.decide_diff) or res.copy_new or res.replace:
        return 1
    print(f"  Nothing in {s['name']} is missing from Proton.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
