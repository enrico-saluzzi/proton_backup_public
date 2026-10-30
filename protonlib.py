#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
protonlib.py - Shared helpers used by the other scripts in this repository,
including the Proton Drive scan used by file-backup.py and drive-compare.py.

Not a standalone script: the other scripts import it. Python automatically
adds the directory of the script being run to sys.path, so `import protonlib`
works with no setup from any working directory, as long as this file sits
next to the script that imports it.
"""

import configparser
import hashlib
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone

DEFAULT_CLI = "proton-drive"
LOG_DIR = os.path.expanduser("~/drive-sync-logs")


def human(n):
    """Format a byte count as a human-readable size (KB, MB, ...)."""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def sha1_of(path, chunk=1 << 20):
    """Sha1 of a file's content, or None if it can't be read."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(chunk), b""):
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def nfc(s):
    """Normalize to NFC for comparisons only.

    On macOS Proton returns names in NFD, and passing the NFC form back to
    the CLI gets you "Node not found". Use this for dictionary keys when
    comparing, never for arguments passed to run_cli().
    """
    return unicodedata.normalize("NFC", s)


# macOS doesn't allow ':' in filenames (an HFS-era leftover, where it was the
# path separator). The CLI replaces it with '_' when it writes a file to disk,
# and does the same with a few other problematic characters. Without this
# normalization, the same file looks both "missing" and "orphaned" when a
# remote inventory is compared with a local backup.
FORBIDDEN_CHARS = {":": "_", "|": "_", "\r": "_", "\n": "_", "*": "_",
                   "?": "_", "<": "_", ">": "_", '"': "_"}


def as_local(rel):
    """Apply to a remote path the same character substitution the CLI
    applies when it writes the file to disk."""
    for a, b in FORBIDDEN_CHARS.items():
        rel = rel.replace(a, b)
    return rel


def run_cli(cli, cmd, timeout, capture=True):
    """Invoke the Proton Drive CLI with stdin closed.

    Without this, a command that asks for confirmation waits on stdin until
    the timeout, or exits 0 without having done anything.
    """
    return subprocess.run([cli] + cmd, capture_output=capture, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout)


# Upload scripts are generated, never run directly by these tools: the user
# reviews them first. They must work with the bash 3.2 shipped with macOS
# (no `set -u` with arrays, no associative arrays).
_UPLOAD_SCRIPT = """#!/bin/bash
{header}
#
# Review this file before running it: nothing has been uploaded yet.
# Files that are empty at run time are skipped: an online-only placeholder
# (0 bytes) would otherwise be uploaded as an empty file. Make the source
# folders available offline first.

CLI={cli}
uploaded=0; skipped=0; failed=0
{functions}
# put DESTINATION FILE...: upload the non-empty files in one CLI call
put() {{
    dest="$1"; shift
    files=()
    for f in "$@"; do
        if [ -s "$f" ]; then
            files+=("$f")
        else
            echo "  skipped, empty or missing (online-only?): $f"
            skipped=$((skipped + 1))
        fi
    done
    [ ${{#files[@]}} -eq 0 ] && return 0
    echo "  uploading ${{#files[@]}} file(s) -> $dest"
    if {upload_cmd} < /dev/null; then
        uploaded=$((uploaded + ${{#files[@]}}))
    else
        echo "  FAILED: upload -> $dest"
        failed=$((failed + ${{#files[@]}}))
    fi
}}

{calls}

echo ""
echo "Uploaded: $uploaded   Skipped: $skipped   Failed: $failed"
echo ""
echo "A zero exit code from the CLI doesn't prove the files arrived."
echo "Verify by running the comparison again:"
{verify}
[ "$failed" -eq 0 ]
"""


def write_upload_script(path, cli, header, upload_cmd, calls, verify, functions=""):
    """Write an executable bash script that uploads files with the CLI.

    header: comment lines describing what the script does.
    upload_cmd: the bash command run by put(); it can use "$CLI",
        "${files[@]}" (the non-empty files) and "$dest".
    calls: list of argument lists; each becomes `put DEST FILE...` (or a
        raw line if it's a string, e.g. a call to a function in `functions`).
    verify: shell commands to show for verifying the result afterwards.
    """
    lines = []
    for c in calls:
        lines.append(c if isinstance(c, str) else
                     "put " + " ".join(shlex.quote(x) for x in c))
    text = _UPLOAD_SCRIPT.format(
        header="\n".join("# " + h if h else "#" for h in header),
        cli=shlex.quote(cli),
        functions=functions,
        upload_cmd=upload_cmd,
        calls="\n".join(lines),
        verify="\n".join("echo " + shlex.quote("    " + v) for v in verify),
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o755)


def generated_on():
    return time.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Proton Drive scan
#
# Enumerates a Drive tree through `proton-drive filesystem list --json`, one
# call per folder, in parallel. The macOS File Provider (the Drive folder in
# Finder) costs ~70 ms per file to enumerate and gives no hashes; the CLI
# costs about a second per folder (half an hour or more for a few thousand
# folders), returns the sha1 of every file and works on headless machines.
#
# The result is a TSV, one row per file:
#     relative_path <TAB> size <TAB> mtime_epoch <TAB> sha1 <TAB> flag
# Size, mtime and sha1 are those of the ORIGINAL file (the "claimed*"
# fields), not of the encrypted blob, so content can be compared without
# downloading anything. flag is "-", UNDECRYPTABLE_NAME (the node uid is used
# as the name) or ENCRYPTED_SIZE (only the encrypted size is known).
#
# A scan writes to <tsv>.partial and replaces <tsv> only when it's complete:
# until then the last complete scan stays usable with --reuse-scan.
#
# Resuming: every folder fully listed is appended to <tsv>.state, every
# folder discovered to <tsv>.queue. Both are removed when the scan completes.
# If they're there and recent, the scan was interrupted (or some folders
# failed): the next scan resumes and lists only the missing folders.
# ---------------------------------------------------------------------------

RESUME_HOURS = 24
REUSE_HINT_MINUTES = 60      # a complete scan younger than this is worth reusing
REUSE_HINT_WAIT = 10         # seconds to read the hint (and Ctrl-C) in a terminal


def scan_path(root, log_dir=LOG_DIR):
    """Where the scan of a remote root is saved, e.g. /my-files -> my-files.tsv."""
    name = root.strip("/").replace("/", "-") or "root"
    return os.path.join(log_dir, name + ".tsv")


def scan_complete(tsv):
    """True if tsv holds a complete scan. A .state file without a .partial
    one is a scan interrupted before .partial existed, written straight into
    the TSV: that TSV is incomplete."""
    if not os.path.exists(tsv):
        return False
    return not (os.path.exists(tsv + ".state") and not os.path.exists(tsv + ".partial"))


def _suggest_reuse(out, root):
    """Say so when the last complete scan is recent: Proton may not have
    changed since, and a scan costs half an hour or more."""
    if not scan_complete(out):
        return
    age = time.time() - os.path.getmtime(out)
    if age > REUSE_HINT_MINUTES * 60:
        return
    minutes = max(1, int(age // 60))
    print(f"  The last complete scan of {root} is {minutes} minute{'s' if minutes > 1 else ''} old.")
    print("  If nothing changed on Proton since, stop now (Ctrl-C) and run again with")
    print("  --reuse-scan. Otherwise wait: it scans again.")
    if not (sys.stdin.isatty() and REUSE_HINT_WAIT):
        return
    try:
        for left in range(REUSE_HINT_WAIT, 0, -1):
            sys.stderr.write(f"\r  Scanning again in {left}s... ")
            sys.stderr.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        sys.exit("Stopped before scanning: run again with --reuse-scan to use the last scan.")
    sys.stderr.write("\r" + " " * 40 + "\r")


def _escape_name(name):
    return name.replace("/", "\\/")


def _to_epoch(iso):
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).timestamp())
    except Exception:
        return 0


class _Scanner:
    def __init__(self, cli, out, state_base, workers, delay, timeout, retries):
        self.cli, self.out = cli, out
        self.workers, self.delay, self.timeout, self.retries = workers, delay, timeout, retries
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.done_folders = 0
        self.files_found = 0
        self.errors = []
        self.undecryptable = []
        self.visited = set()
        self.state_path = state_base + ".state"
        self.queue_path = state_base + ".queue"
        self.pending = 0
        self.pending_cv = threading.Condition()

    def list_folder(self, remote_path):
        cmd = [self.cli, "filesystem", "list", remote_path, "--json"]
        last_err = None
        for attempt in range(self.retries):
            if self.delay:
                time.sleep(self.delay)
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                last_err = f"no response within {self.timeout}s"
                time.sleep(2 ** attempt)
                continue
            if r.returncode != 0:
                last_err = (r.stderr or r.stdout or "").strip()[:200]
                time.sleep(2 ** attempt)
                continue
            out = r.stdout.strip()
            if not out:
                return []
            try:
                return json.loads(out)
            except json.JSONDecodeError as e:
                last_err = f"invalid JSON: {e}"
                time.sleep(2 ** attempt)
        raise RuntimeError(last_err or "unknown error")

    def process(self, remote_path, rel_path, fh):
        try:
            entries = self.list_folder(remote_path)
        except Exception as e:
            with self.lock:
                self.errors.append((remote_path, str(e)))
            return
        rows, subdirs = [], []
        for e in entries:
            name_obj = e.get("name") or {}
            ok = name_obj.get("ok", False)
            name = name_obj.get("value") if ok else None
            if not ok or name is None:
                # the name couldn't be decrypted: the uid is used instead
                name = e.get("uid", "?")
                with self.lock:
                    self.undecryptable.append(rel_path + "/" + name)
                flag = "UNDECRYPTABLE_NAME"
            else:
                flag = "-"
            child_rel = (rel_path + "/" + name) if rel_path else name
            if e.get("type") == "folder":
                subdirs.append((remote_path.rstrip("/") + "/" + _escape_name(name), child_rel))
                continue
            rev = e.get("activeRevision") or {}
            size = rev.get("claimedSize")
            if size is None:
                size = e.get("totalStorageSize", 0)
                if flag == "-":
                    flag = "ENCRYPTED_SIZE"
            mtime = _to_epoch(rev.get("claimedModificationTime") or e.get("modificationTime"))
            sha1 = ((rev.get("claimedDigests") or {}).get("sha1")) or "-"
            rows.append(f"{child_rel}\t{size}\t{mtime}\t{sha1}\t{flag}\n")
        with self.lock:
            if rows:
                fh.writelines(rows)
                fh.flush()
                self.files_found += len(rows)
            self.done_folders += 1
            with open(self.state_path, "a") as st:
                st.write(remote_path + "\n")
        for sub_remote, sub_rel in subdirs:
            self.enqueue(sub_remote, sub_rel)

    def enqueue(self, remote_path, rel_path):
        with self.pending_cv:
            if remote_path in self.visited:
                return
            self.visited.add(remote_path)
            self.pending += 1
        with self.lock:
            with open(self.queue_path, "a") as qf:
                qf.write(remote_path + "\t" + rel_path + "\n")
        self.q.put((remote_path, rel_path))

    def worker(self, fh):
        while True:
            try:
                remote_path, rel_path = self.q.get(timeout=0.5)
            except queue.Empty:
                with self.pending_cv:
                    if self.pending == 0:
                        return
                continue
            try:
                self.process(remote_path, rel_path, fh)
            finally:
                with self.pending_cv:
                    self.pending -= 1
                    if self.pending == 0:
                        self.pending_cv.notify_all()
                self.q.task_done()

    def progress_loop(self, stop):
        t0 = time.time()
        while not stop.wait(2):
            with self.lock:
                d, f, e = self.done_folders, self.files_found, len(self.errors)
            sys.stderr.write(f"\r  scanning Proton: {d} folders, {f} files, {e} errors "
                             f"| {int(time.time() - t0)}s | queued: {self.q.qsize()}   ")
            sys.stderr.flush()

    def run(self, root, resume):
        todo = []
        mode = "w"
        if resume:
            with open(self.state_path) as st:
                already = set(l.rstrip("\n") for l in st if l.strip())
            seen = set()
            if os.path.exists(self.queue_path):
                with open(self.queue_path) as qf:
                    for line in qf:
                        parts = line.rstrip("\n").split("\t", 1)
                        if len(parts) == 2 and parts[0] not in already and parts[0] not in seen:
                            seen.add(parts[0])
                            todo.append((parts[0], parts[1]))
            self.visited |= already
            mode = "a"
        else:
            for p in (self.out, self.state_path, self.queue_path):
                if os.path.exists(p):
                    os.remove(p)
        with open(self.out, mode) as fh:
            if todo:
                for rp, rel in todo:
                    self.enqueue(rp, rel)
            elif not resume:
                self.enqueue(root, "")
            stop = threading.Event()
            threading.Thread(target=self.progress_loop, args=(stop,), daemon=True).start()
            threads = [threading.Thread(target=self.worker, args=(fh,))
                       for _ in range(self.workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stop.set()
        sys.stderr.write("\n")


def scan_drive(cli, root, workers=3, delay=0.2, timeout=300, retries=3, log_dir=LOG_DIR):
    """Scan a Drive tree into a TSV; returns (tsv_path, errors).

    errors lists the folders that couldn't be read after all retries: the scan
    is then incomplete, and calling scan_drive again resumes it.
    """
    os.makedirs(log_dir, exist_ok=True)
    out = scan_path(root, log_dir)
    partial = out + ".partial"
    state = out + ".state"
    # a scan interrupted before .partial existed wrote straight into the TSV:
    # carry it over, so it resumes instead of starting again
    if os.path.exists(state) and os.path.exists(out) and not os.path.exists(partial):
        os.replace(out, partial)
    resume = (os.path.exists(state) and os.path.exists(partial)
              and time.time() - os.path.getmtime(state) < RESUME_HOURS * 3600)
    if resume:
        print(f"  Resuming the interrupted scan of {root}...")
    else:
        _suggest_reuse(out, root)
        print(f"  Scanning {root} on Proton Drive...")
    try:
        subprocess.run([cli, "--help"], capture_output=True, timeout=30)
    except FileNotFoundError:
        sys.exit(f"ERROR: '{cli}' not found. Use --cli to point at it.")
    except Exception:
        pass
    t0 = time.time()
    sc = _Scanner(cli, partial, out, workers, delay, timeout, retries)
    sc.run(root, resume)
    if sc.undecryptable:
        print(f"  WARNING: {len(sc.undecryptable)} names on Proton couldn't be decrypted "
              "(listed with their uid).")
    if sc.errors:
        return out, sc.errors
    os.replace(partial, out)
    for p in (out + ".state", out + ".queue"):
        if os.path.exists(p):
            os.remove(p)
    print(f"  Scan complete in {int(time.time() - t0)}s: {out}")
    return out, []


def scan_errors_exit(errors):
    """Stop the run after an incomplete scan, explaining how to resume it."""
    print("")
    print(f"  ERROR: {len(errors)} folders on Proton couldn't be read, e.g.:")
    for path, err in errors[:5]:
        print(f"    {path}: {err[:120]}")
    print("")
    print("  Comparing with an incomplete scan would report files as missing that")
    print("  aren't. Run the same command again: the scan resumes from where it")
    print("  stopped and only lists the missing folders.")
    sys.exit(1)


def read_scan(path):
    """Rows of a scan TSV as (relative_path, size, mtime, sha1, flag)."""
    rows = []
    with open(path, encoding="utf-8", newline="\n") as f:
        for line in f:
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) < 5 or not parts[0].strip():
                continue
            try:
                size, mtime = int(parts[1] or 0), int(parts[2] or 0)
            except ValueError:
                size, mtime = 0, 0
            rows.append((parts[0], size, mtime, parts[3], parts[4]))
    return rows


# ---------------------------------------------------------------------------
# Configuration file and profiles
#
# Scripts that take profiles read them from one INI file,
# ~/.config/proton-backup/config.ini (or --config). A profile is a section
# named "<script> <profile>", e.g. [drive-compare dropbox] or
# [file-backup ssd]; [DEFAULT] holds values shared by every profile. Each
# setting, its default and its explanation are declared here, in
# PROFILE_SETTINGS, so that any script can rewrite the whole file with the
# right comments. Command-line options always override the profile.
# ---------------------------------------------------------------------------

CONFIG_DIR = os.path.expanduser("~/.config/proton-backup")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.ini")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# script -> [(setting, default, explanation)], in the order they're written
PROFILE_SETTINGS = {
    "drive-compare": [
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
    ],
    "file-backup": [
        ("dest", "",
         "the backup folder, e.g. on an external disk"),
        ("proton_root", "/my-files",
         "the Proton folder to back up (/my-files is the whole Drive)"),
        ("verify", "yes",
         "yes = recompute the sha1 of every local file: the only check that "
         "catches a corrupted file of the same size (slower)"),
        ("workers", "3",
         "parallel calls to the CLI while scanning Proton"),
        ("batch", "20",
         "files per download call"),
        ("timeout", "1800",
         "seconds before a download call is given up (it's retried on the next run)"),
    ],
    "photo-backup": [
        ("dest", "",
         "the photo backup folder (one subfolder per capture year), e.g. on an "
         "external disk"),
        ("albums", "no",
         "yes = also rebuild every album as a folder of hard links to the photos"),
        ("album_dir", "album",
         "where the album folders go: relative to dest, or an absolute path on "
         "the same disk (hard links can't cross disks)"),
        ("batch", "5",
         "photos per CLI call (bigger batches time out more)"),
        ("timeout", "300",
         "seconds before a CLI call is given up (failed photos are retried on "
         "the next run)"),
    ],
    "photo-compare": [
        ("folder", "",
         "the local photo folder to check against Proton Photos (e.g. a "
         "Camera Uploads folder), searched recursively"),
        ("tz", "",
         "time zone of capture times written without one, e.g. Europe/Rome "
         "(empty = this Mac's)"),
        ("tolerance", "1",
         "seconds of tolerance on capture times (0 = exact)"),
        ("exif", "yes",
         "yes = read EXIF dates with exiftool when the file name has none"),
        ("upload_script", "no",
         "yes = also write a script that uploads the missing photos"),
        ("batch", "20",
         "photos per upload command in the upload script"),
    ],
    "photo-prune": [
        ("timeout", "120",
         "seconds before a CLI call is given up"),
    ],
    "photo-redate": [
        ("exclude", "",
         "album names to leave alone, comma-separated"),
        ("exclude_file", "",
         "file with one album name to leave alone per line (# for comments)"),
        ("wait", "20",
         "with --apply: seconds to wait before looking for the re-uploaded photos"),
        ("timeout", "600",
         "seconds before a CLI call is given up"),
    ],
}
# explanations for settings in [DEFAULT], shared by several scripts
SHARED_HELP = {
    "proton_root": "the Proton folder to work on (/my-files is the whole Drive)",
}
SETTING_CHOICES = {
    "compare_by": ("content", "size"),
    "winner": ("proton", "folder", "newer", "ask"),
    "moved": ("hide", "list"),
    "proton_only": ("hide", "list"),
    "copy_script": ("yes", "no"),
    "verify": ("yes", "no"),
    "albums": ("yes", "no"),
    "exif": ("yes", "no"),
    "upload_script": ("yes", "no"),
}
SETTING_MINIMUM = {"review_max": 0, "workers": 1, "batch": 1, "timeout": 1,
                   "tolerance": 0, "wait": 0}

# Scripts that work on another script's backup folder take it from the
# profile of the same name there: photo-prune.py ssd and photo-redate.py ssd
# use the dest of [photo-backup ssd]. Their own sections are optional and
# hold only their own settings, so the folder is written in one place.
PROFILE_BASE = {"photo-prune": "photo-backup", "photo-redate": "photo-backup"}


def setting_help(key):
    for settings in PROFILE_SETTINGS.values():
        for k, _, h in settings:
            if k == key:
                return h
    return None


def profile_defaults(script):
    return {k: d for k, d, _ in PROFILE_SETTINGS[script]}


def read_config(path):
    cfg = configparser.ConfigParser(interpolation=None,
                                    inline_comment_prefixes=(";", "#"))
    if os.path.exists(path):
        try:
            cfg.read(path, encoding="utf-8")
        except configparser.Error as e:
            sys.exit(f"ERROR: can't read {path}:\n  {e}")
    return cfg


def profile_section(script, name):
    return f"{script} {name}"


def profile_names(cfg, script):
    prefix = script + " "
    return [s[len(prefix):] for s in cfg.sections() if s.startswith(prefix)]


def render_config(cfg):
    """The configuration as text, every setting preceded by its explanation."""
    out = ["# Profiles for the proton_backup scripts: one section per profile,",
           "# named [<script> <profile>], e.g. [drive-compare dropbox] or [file-backup ssd].",
           "# Run:   ./<script>.py <profile>   (or just ./<script>.py with a single profile)",
           "# Edit:  ./<script>.py --setup <profile>, or by hand.",
           "# Settings in [DEFAULT] apply to every profile.", ""]

    def block(items, script):
        own_help = ({k: h for k, _, h in PROFILE_SETTINGS[script]}
                    if script in PROFILE_SETTINGS else dict(SHARED_HELP))
        known = list(own_help)
        for k in known + sorted(set(items) - set(known)):
            if k not in items:
                continue
            h = own_help.get(k) or setting_help(k)
            if h:
                out.append(f"; {h}")
            out.append(f"{k} = {items[k]}")
        out.append("")

    if cfg.defaults():
        out.append("[DEFAULT]")
        block(dict(cfg.defaults()), None)
    for section in cfg.sections():
        out.append(f"[{section}]")
        # only the profile's own keys, not those inherited from [DEFAULT]
        own = {k: v for k, v in cfg.items(section, raw=True)
               if k not in cfg.defaults() or cfg.defaults()[k] != v}
        block(own, section.split(" ", 1)[0])
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
    """Validate choices and numbers in place (numbers become ints)."""
    for k, allowed in SETTING_CHOICES.items():
        if k in s and s[k] not in allowed:
            sys.exit(f"ERROR: {where}: {k} = {s[k]!r}, expected one of: {', '.join(allowed)}")
    for k, low in SETTING_MINIMUM.items():
        if k not in s:
            continue
        try:
            s[k] = int(s[k])
            if s[k] < low:
                raise ValueError
        except ValueError:
            sys.exit(f"ERROR: {where}: {k} must be a number, {low} or more")


def choose_profile(cfg, script, target, config_path):
    """The profile to use, or None when target is a path to take as is.

    With no target, the only profile of this script is used. A target that
    looks like a path ("/", "~", ".") is never a profile."""
    base = PROFILE_BASE.get(script, script)
    names = profile_names(cfg, base)
    if target is None:
        if len(names) == 1:
            return names[0]
        if not names:
            sys.exit(f"ERROR: no {base} profile in {config_path}.\n"
                     f"  Create one with: ./{base}.py --setup NAME (or pass a folder)")
        sys.exit(f"ERROR: several {base} profiles in {config_path}: "
                 f"{', '.join(names)}. Name the one to use.")
    path_like = "/" in target or target.startswith(("~", "."))
    if not path_like and target in names:
        return target
    if path_like or os.path.isdir(os.path.expanduser(target)):
        return None
    if cfg.has_section(target):
        sys.exit(f"ERROR: [{target}] in {config_path} is in the old format: rename the "
                 f"section [{profile_section(base, target)}].")
    sys.exit(f"ERROR: '{target}' is neither a {base} profile in {config_path} "
             f"(profiles: {', '.join(names) or 'none yet'}) nor a folder.\n"
             f"  Create the profile with: ./{base}.py --setup {target}")


def load_settings(cfg, script, name):
    """Defaults, then [DEFAULT], then the profile (if any). For a script in
    PROFILE_BASE, the profile's dest comes from the base script's section,
    and its own section is optional."""
    s = profile_defaults(script)
    s.update(cfg.defaults())
    if name is None:
        return s
    base = PROFILE_BASE.get(script)
    if base:
        s["dest"] = cfg.get(profile_section(base, name), "dest", fallback="")
    own = profile_section(script, name)
    if cfg.has_section(own):
        s.update(cfg.items(own))
    return s


def ask(question, default, choices=None, minimum=None):
    """One setup question. End of input means "quit", as Ctrl-C does."""
    hint = f" [{'/'.join(choices)}]" if choices else ""
    while True:
        try:
            ans = input(f"  {question}{hint} ({default}): ").strip()
        except EOFError:
            raise KeyboardInterrupt
        ans = ans or str(default)
        if choices and ans not in choices:
            print(f"    answer one of: {', '.join(choices)}")
            continue
        if minimum is not None and not (ans.isdigit() and int(ans) >= minimum):
            print(f"    a number, {minimum} or more")
            continue
        return ans


def ask_existing_folder(key, default):
    """Ask for a folder; a missing one can be kept (an unplugged disk)."""
    while True:
        folder = ask(key, default)
        if os.path.isdir(os.path.expanduser(folder)):
            return folder
        print(f"    {folder} doesn't exist (an external disk not plugged in?)")
        if ask("keep it anyway?", "no", ("yes", "no")) == "yes":
            return folder


def run_setup(config_path, script, name, questions):
    """Create or change a profile of `script`. questions(current) asks the
    script's own questions and returns {setting: value}."""
    if not sys.stdin.isatty():
        sys.exit("ERROR: --setup asks questions: run it in a terminal.")
    cfg = read_config(config_path)
    print("")
    print(f"{script} setup  (configuration: {config_path})")
    names = profile_names(cfg, script)
    if names:
        print(f"  existing {script} profiles: {', '.join(names)}")
    print("  Press Enter to keep the value in brackets. Ctrl-C to quit without saving.")
    print("")
    section = None
    try:
        while not name or not PROFILE_RE.match(name):
            if name:
                print("    letters, digits, '.', '_' or '-' only, e.g. dropbox or ssd")
            name = input("  Profile name, e.g. dropbox or ssd: ").strip()
        section = profile_section(script, name)
        cur = profile_defaults(script)
        cur.update(cfg.defaults())
        if cfg.has_section(section):
            cur.update(cfg.items(section, raw=True))
            print(f"  Changing profile [{section}].")
        else:
            print(f"  New profile [{section}].")
        new = questions(cur)
    except (KeyboardInterrupt, EOFError):
        print("")
        sys.exit("Setup interrupted: nothing written.")

    if not cfg.has_section(section):
        cfg.add_section(section)
    for k, v in new.items():
        cfg.set(section, k, str(v))
    write_config(config_path, cfg)
    print("")
    print(f"  Saved profile [{section}] in {config_path}")
    others = profile_names(cfg, script)
    print(f"  Run it with:  ./{script}.py" + (f" {name}" if len(others) > 1 else
                                              f"   (or ./{script}.py {name})"))
    return 0
