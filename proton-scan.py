#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
proton-scan.py - Enumerate a Proton Drive tree using the official CLI.

WHY
  The macOS File Provider extension (the Drive mount in Finder) is far too
  slow to enumerate a large archive: on a 44,000-file Drive, listing through
  the File Provider cost roughly 70 ms per file, against about 0.05 ms
  through the CLI. That is the difference between seconds and hours. This
  script bypasses the File Provider entirely and talks to the API through
  `proton-drive filesystem list --json`, one call per folder, in parallel.
  It also works on headless machines (NAS, servers) with no Finder at all.

OUTPUT
  A TSV file with one row per file:

      relative_path <TAB> size <TAB> mtime_epoch <TAB> sha1 <TAB> flag

  Size, mtime and sha1 are those of the ORIGINAL file (the "claimed*" fields),
  not of the encrypted blob, so the sha1 lets other scripts compare content
  without downloading anything. The flag column is "-" for normal rows,
  UNDECRYPTABLE_NAME when the name couldn't be decrypted (the node UID is
  used instead), or ENCRYPTED_SIZE when only the encrypted size is known.

  This TSV is the input of file-backup.py, drive-compare.py and
  proton-orphans.py.

RESUMING
  Every folder that is fully listed is appended to <output>.state, and every
  folder discovered is appended to <output>.queue. If the run is interrupted,
  or some folders fail after all retries, rerun the same command with
  --resume: only the folders not yet completed are listed again.

USAGE
  ./proton-scan.py /my-files/Books -o books.tsv
  ./proton-scan.py /my-files -o proton.tsv --workers 6
  ./proton-scan.py /my-files -o proton.tsv --resume
  ./proton-scan.py /my-files --workers 3 --delay 0.2 --timeout 300   # gentle

SAFETY
  Read-only. Doesn't download, modify or delete anything.
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import protonlib

VERSION = "1.4"


def parse_args():
    p = argparse.ArgumentParser(
        description="Enumerate a Proton Drive tree via the official CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("root", help="remote starting path, e.g. /my-files/Books")
    p.add_argument("-o", "--out", default=None,
                   help="output TSV file. A bare name ends up in --log-dir; "
                        "if omitted, the name is derived from the remote path")
    p.add_argument("--log-dir", default=os.path.expanduser("~/drive-sync-logs"),
                   help="where TSV and state files go (default ~/drive-sync-logs)")
    p.add_argument("--workers", type=int, default=6,
                   help="parallel calls (default 6; raising it risks rate limiting)")
    p.add_argument("--delay", type=float, default=0.0,
                   help="seconds to wait before each call (anti rate-limit)")
    p.add_argument("--retries", type=int, default=3, help="attempts per folder")
    p.add_argument("--cli", default=protonlib.DEFAULT_CLI, help="path to the CLI binary")
    p.add_argument("--resume", action="store_true",
                   help="resume using the state file of an interrupted run")
    p.add_argument("--timeout", type=int, default=60,
                   help="timeout per call, in seconds")
    a = p.parse_args()

    # Everything lands in one place: no files scattered around the home folder.
    os.makedirs(a.log_dir, exist_ok=True)
    if not a.out:
        name = a.root.strip("/").replace("/", "-") or "root"
        a.out = name + ".tsv"
    if os.sep not in a.out:
        a.out = os.path.join(a.log_dir, a.out)
    a.out = os.path.expanduser(a.out)
    return a


# ---------------------------------------------------------------------------
# Remote paths: names can contain "/", which must be escaped with a backslash
# ---------------------------------------------------------------------------

def escape_name(name):
    return name.replace("/", "\\/")


def join_remote(parent, name):
    return parent.rstrip("/") + "/" + escape_name(name)


def to_epoch(iso):
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).timestamp())
    except Exception:
        return 0


# ---------------------------------------------------------------------------


class Scanner:
    def __init__(self, args):
        self.args = args
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.done_folders = 0
        self.files_found = 0
        self.errors = []
        self.undecryptable = []
        self.visited = set()
        self.state_path = args.out + ".state"
        self.queue_path = args.out + ".queue"
        self.pending = 0
        self.pending_cv = threading.Condition()

    # -- calling the CLI -----------------------------------------------------

    def list_folder(self, remote_path):
        cmd = [self.args.cli, "filesystem", "list", remote_path, "--json"]
        last_err = None
        for attempt in range(self.args.retries):
            if self.args.delay:
                time.sleep(self.args.delay)
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL,
                                   timeout=self.args.timeout)
            except subprocess.TimeoutExpired:
                last_err = f"no response within {self.args.timeout}s"
                with self.lock:
                    if len(self.errors) < 3:
                        sys.stderr.write(
                            f"\n  TIMEOUT ({self.args.timeout}s) on {remote_path} "
                            f"[attempt {attempt + 1}/{self.args.retries}]\n")
                        sys.stderr.flush()
                time.sleep(2 ** attempt)
                continue
            if r.returncode != 0:
                last_err = (r.stderr or r.stdout or "").strip()[:200]
                if attempt == 0:
                    with self.lock:
                        if len(self.errors) < 3:
                            sys.stderr.write(
                                f"\n  attempt failed on {remote_path}: {last_err[:200]}\n")
                            sys.stderr.flush()
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

    # -- processing one folder -----------------------------------------------

    def process(self, remote_path, rel_path, fh):
        try:
            entries = self.list_folder(remote_path)
        except Exception as e:
            with self.lock:
                self.errors.append((remote_path, str(e)))
                if len(self.errors) <= 3:
                    sys.stderr.write(f"\n  ERROR on {remote_path}: {str(e)[:300]}\n")
                    sys.stderr.flush()
            return

        rows = []
        subdirs = []
        for e in entries:
            name_obj = e.get("name") or {}
            ok = name_obj.get("ok", False)
            name = name_obj.get("value") if ok else None
            if not ok or name is None:
                # the name couldn't be decrypted: the UID is used as identifier
                name = e.get("uid", "?")
                with self.lock:
                    self.undecryptable.append(rel_path + "/" + name)
                flag = "UNDECRYPTABLE_NAME"
            else:
                flag = "-"

            child_rel = (rel_path + "/" + name) if rel_path else name

            if e.get("type") == "folder":
                subdirs.append((join_remote(remote_path, name), child_rel))
                continue

            rev = e.get("activeRevision") or {}
            size = rev.get("claimedSize")
            if size is None:
                size = e.get("totalStorageSize", 0)
                if flag == "-":
                    flag = "ENCRYPTED_SIZE"
            mtime = to_epoch(rev.get("claimedModificationTime")
                             or e.get("modificationTime"))
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

    # -- queue ---------------------------------------------------------------

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

    # -- progress ------------------------------------------------------------

    def progress_loop(self, stop):
        t0 = time.time()
        while not stop.is_set():
            time.sleep(2)
            with self.lock:
                d, f, e = self.done_folders, self.files_found, len(self.errors)
            el = int(time.time() - t0)
            rate = d / el if el else 0
            sys.stderr.write(
                f"\r  {d} folders, {f} files, {e} errors "
                f"| {el}s | {rate:.1f} folders/s | queued: {self.q.qsize()}   ")
            sys.stderr.flush()

    # -- run -----------------------------------------------------------------

    def run(self):
        a = self.args
        mode = "a"
        todo = []
        if a.resume and os.path.exists(self.state_path):
            with open(self.state_path) as st:
                already = set(l.rstrip("\n") for l in st if l.strip())
            discovered = []
            if os.path.exists(self.queue_path):
                with open(self.queue_path) as qf:
                    for line in qf:
                        parts = line.rstrip("\n").split("\t", 1)
                        if len(parts) == 2:
                            discovered.append((parts[0], parts[1]))
            seen = set()
            for rp, rel in discovered:
                if rp not in already and rp not in seen:
                    seen.add(rp)
                    todo.append((rp, rel))
            self.visited |= already
            sys.stderr.write(
                f"Resuming: {len(already)} folders already done, "
                f"{len(todo)} left to recover.\n")
        else:
            for p in (a.out, self.state_path, self.queue_path):
                if os.path.exists(p):
                    os.remove(p)
            mode = "w"

        sys.stderr.write("  first CLI call in progress...\n")
        sys.stderr.flush()

        with open(a.out, mode) as fh:
            if todo:
                for rp, rel in todo:
                    self.enqueue(rp, rel)
            else:
                self.enqueue(a.root, "")
            stop = threading.Event()
            prog = threading.Thread(target=self.progress_loop, args=(stop,), daemon=True)
            prog.start()

            threads = [threading.Thread(target=self.worker, args=(fh,))
                       for _ in range(a.workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stop.set()
            time.sleep(0.1)

        sys.stderr.write("\n")
        return self.report()

    def report(self):
        print("")
        print(f"  Folders listed : {self.done_folders}")
        print(f"  Files found    : {self.files_found}")
        print(f"  Output         : {self.args.out}")

        if self.undecryptable:
            print("")
            print(f"  WARNING: {len(self.undecryptable)} names could not be decrypted.")
            for u in self.undecryptable[:10]:
                print(f"    {u}")
            if len(self.undecryptable) > 10:
                print(f"    ... and {len(self.undecryptable) - 10} more")

        if self.errors:
            print("")
            print(f"  {len(self.errors)} folders NOT read:")
            for path, err in self.errors[:10]:
                print(f"    {path}: {err}")
            if len(self.errors) > 10:
                print(f"    ... and {len(self.errors) - 10} more")
            print("")
            print("  The listing is incomplete. Rerun with --resume to retry")
            print("  only the missing folders.")
            return 1

        print("")
        print("  Listing complete.")
        return 0


def main():
    args = parse_args()
    sys.stderr.write(
        f"proton-scan.py v{VERSION}  |  cli={args.cli}  workers={args.workers}  "
        f"delay={args.delay}s  timeout={args.timeout}s  retries={args.retries}\n")
    sys.stderr.write(f"  root:   {args.root}\n  output: {args.out}\n\n")
    sys.stderr.flush()
    try:
        subprocess.run([args.cli, "--help"], capture_output=True, timeout=30)
    except FileNotFoundError:
        sys.exit(f"ERROR: '{args.cli}' not found. Use --cli to point at it.")
    except Exception:
        pass
    sys.exit(Scanner(args).run())


if __name__ == "__main__":
    main()
