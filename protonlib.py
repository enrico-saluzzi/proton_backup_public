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

import hashlib
import json
import os
import queue
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
# Finder) costs ~70 ms per file to enumerate, the CLI ~0.05 ms: seconds
# instead of hours on a large tree, and it works on headless machines too.
#
# The result is a TSV, one row per file:
#     relative_path <TAB> size <TAB> mtime_epoch <TAB> sha1 <TAB> flag
# Size, mtime and sha1 are those of the ORIGINAL file (the "claimed*"
# fields), not of the encrypted blob, so content can be compared without
# downloading anything. flag is "-", UNDECRYPTABLE_NAME (the node uid is used
# as the name) or ENCRYPTED_SIZE (only the encrypted size is known).
#
# Resuming: every folder fully listed is appended to <tsv>.state, every
# folder discovered to <tsv>.queue. Both are removed when the scan completes.
# If they're there and recent, the scan was interrupted (or some folders
# failed): the next scan resumes and lists only the missing folders.
# ---------------------------------------------------------------------------

RESUME_HOURS = 24


def scan_path(root, log_dir=LOG_DIR):
    """Where the scan of a remote root is saved, e.g. /my-files -> my-files.tsv."""
    name = root.strip("/").replace("/", "-") or "root"
    return os.path.join(log_dir, name + ".tsv")


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
    def __init__(self, cli, out, workers, delay, timeout, retries):
        self.cli, self.out = cli, out
        self.workers, self.delay, self.timeout, self.retries = workers, delay, timeout, retries
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.done_folders = 0
        self.files_found = 0
        self.errors = []
        self.undecryptable = []
        self.visited = set()
        self.state_path = out + ".state"
        self.queue_path = out + ".queue"
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
    state = out + ".state"
    resume = (os.path.exists(state) and os.path.exists(out)
              and time.time() - os.path.getmtime(state) < RESUME_HOURS * 3600)
    if resume:
        print(f"  Resuming the interrupted scan of {root}...")
    else:
        print(f"  Scanning {root} on Proton Drive...")
    try:
        subprocess.run([cli, "--help"], capture_output=True, timeout=30)
    except FileNotFoundError:
        sys.exit(f"ERROR: '{cli}' not found. Use --cli to point at it.")
    except Exception:
        pass
    t0 = time.time()
    sc = _Scanner(cli, out, workers, delay, timeout, retries)
    sc.run(root, resume)
    if sc.undecryptable:
        print(f"  WARNING: {len(sc.undecryptable)} names on Proton couldn't be decrypted "
              "(listed with their uid).")
    if sc.errors:
        return out, sc.errors
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
