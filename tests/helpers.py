# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
Shared helpers for the unit tests: a fake proton-drive CLI, and a base test
case that loads protonlib (and optionally a script) fresh with HOME in a
temporary folder, so nothing touches the real ~/drive-sync-logs,
~/.config/proton-backup or the Proton account.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_CLI = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Fake proton-drive. $FAKE_STATE is a JSON file:
    {"drive": {"/my-files/a.txt": "content" or ["content", "2024-01-01T00:00:00Z"]},
     "trash": [{"type": "file", "name": "x", "content": "...", "when": "2026-10-12T10:00:00Z"},
               {"type": "folder", "name": "Old", "when": "..."}],
     "trash_fails": false,
     "fail_download": ["/my-files/remote path that makes a download call fail"]}
    Every call is appended as a JSON list to $FAKE_STATE.calls."""
    import hashlib, json, os, sys
    st = json.load(open(os.environ["FAKE_STATE"], encoding="utf-8"))
    args = sys.argv[1:]
    with open(os.environ["FAKE_STATE"] + ".calls", "a", encoding="utf-8") as f:
        f.write(json.dumps(args, ensure_ascii=False) + "\\n")

    def content_of(v):
        return (v, "2024-01-01T00:00:00Z") if isinstance(v, str) else tuple(v)

    def node(name, content, mtime):
        b = content.encode()
        return {"name": {"ok": True, "value": name}, "type": "file", "uid": "f-" + name,
                "activeRevision": {"claimedSize": len(b), "claimedModificationTime": mtime,
                                   "claimedDigests": {"sha1": hashlib.sha1(b).hexdigest()}}}

    if args[:1] == ["--help"]:
        print("fake proton-drive"); sys.exit(0)
    if args[:3] == ["filesystem", "list", "/trash"]:
        if st.get("trash_fails"):
            print("network error", file=sys.stderr); sys.exit(1)
        out = []
        for t in st.get("trash", []):
            if t["type"] == "file":
                n = node(t["name"], t["content"], "2024-01-01T00:00:00Z")
            else:
                n = {"name": {"ok": True, "value": t["name"]}, "type": "folder", "uid": "d"}
            n["trashTime"] = t["when"]
            out.append(n)
        print(json.dumps(out)); sys.exit(0)
    if args[:2] == ["filesystem", "list"]:
        base = args[2].replace("\\\\/", "/").rstrip("/")
        out, folders = [], set()
        for path, v in st["drive"].items():
            content, mtime = content_of(v)
            if not path.startswith(base + "/"):
                continue
            rest = path[len(base) + 1:]
            if "/" in rest:
                folders.add(rest.split("/", 1)[0])
            else:
                out.append(node(rest, content, mtime))
        for d in sorted(folders):
            out.append({"name": {"ok": True, "value": d}, "type": "folder", "uid": "d-" + d})
        print(json.dumps(out)); sys.exit(0)
    if args[:2] == ["filesystem", "download"]:
        rest = [a for a in args[2:]]
        # options: -f STRATEGY -d STRATEGY
        while rest and rest[0] in ("-f", "-d"):
            rest = rest[2:]
        paths, target = rest[:-1], rest[-1]
        if any(p in st.get("fail_download", []) for p in paths):
            print("download failed: network error", file=sys.stderr); sys.exit(1)
        for p in paths:
            content, _ = content_of(st["drive"][p])
            name = p.rsplit("/", 1)[1]
            for ch in ':|*?<>"':          # what the real CLI does on macOS
                name = name.replace(ch, "_")
            with open(os.path.join(target, name), "w", encoding="utf-8") as f:
                f.write(content)
        sys.exit(0)
    print("unknown: " + " ".join(args), file=sys.stderr); sys.exit(2)
''')


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def sha1(text):
    return hashlib.sha1(text.encode()).hexdigest()


def epoch(day):
    """Epoch seconds of a UTC date such as 2024-01-01."""
    import calendar
    return calendar.timegm(time.strptime(day, "%Y-%m-%d"))


class FakeStdin:
    """Stand-in for sys.stdin: only isatty() is used, input() is patched."""

    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ScriptTestCase(unittest.TestCase):
    """Loads protonlib, and SCRIPT if set, fresh with HOME in a temp folder.

    self.plib is protonlib, self.mod the script (None if SCRIPT is None),
    self.config the default configuration file under the temp HOME."""

    SCRIPT = None          # e.g. ("drive_compare", "drive-compare.py")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        env = mock.patch.dict(os.environ, {"HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        saved = sys.modules.get("protonlib")
        self.addCleanup(self._restore_protonlib, saved)

        self.plib = load_module("protonlib", "protonlib.py")
        sys.modules["protonlib"] = self.plib
        self.mod = load_module(*self.SCRIPT) if self.SCRIPT else None

        # sanity: everything the modules will write lives under the temp HOME
        self.assertTrue(self.plib.CONFIG_DIR.startswith(self.home))
        self.assertTrue(self.plib.CONFIG_PATH.startswith(self.home))
        self.assertTrue(self.plib.LOG_DIR.startswith(self.home))
        if self.mod is not None and hasattr(self.mod, "CLOUD_STORAGE"):
            self.assertTrue(self.mod.CLOUD_STORAGE.startswith(self.home))
        self.config = os.path.join(self.home, ".config", "proton-backup", "config.ini")
        self.assertEqual(self.plib.CONFIG_PATH, self.config)

    @staticmethod
    def _restore_protonlib(saved):
        if saved is None:
            sys.modules.pop("protonlib", None)
        else:
            sys.modules["protonlib"] = saved

    # -- files --------------------------------------------------------------

    def mkdir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def write(self, path, text, mtime=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def write_config(self, text, path=None):
        path = path or self.config
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(text))
        return path

    def fake_cli(self):
        """Write the fake proton-drive; returns its path. State goes in
        self.state ($FAKE_STATE), calls are logged to self.state + '.calls'."""
        cli = self.write(os.path.join(self.tmp, "bin", "proton-drive"), FAKE_CLI)
        os.chmod(cli, 0o755)
        self.state = os.path.join(self.tmp, "state.json")
        env = mock.patch.dict(os.environ, {"FAKE_STATE": self.state})
        env.start()
        self.addCleanup(env.stop)
        return cli

    def cli_calls(self, *prefix):
        try:
            with open(self.state + ".calls", encoding="utf-8") as f:
                calls = [json.loads(line) for line in f]
        except OSError:
            return []
        return [c for c in calls if c[:len(prefix)] == list(prefix)]

    # -- running ------------------------------------------------------------

    def assertExits(self, fn, *needles):
        """fn() must stop with sys.exit, and the message must contain needles."""
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                fn()
        msg = str(cm.exception.code)
        for n in needles:
            self.assertIn(n, msg)
        return msg

    def answering(self, answers, tty=True):
        """Context manager: input() returns `answers` in order (an exception
        instance is raised instead), sys.stdin.isatty() returns tty. Fails
        the test on an unexpected question. Yields a list collecting the
        prompts; check `leftover()` afterwards."""
        it = iter(answers)
        prompts = []
        test = self

        def fake_input(prompt=""):
            prompts.append(prompt)
            try:
                ans = next(it)
            except StopIteration:
                test.fail(f"unexpected question: {prompt!r}")
            if isinstance(ans, BaseException):
                raise ans
            return ans

        @contextlib.contextmanager
        def cm():
            with mock.patch("builtins.input", fake_input), \
                    mock.patch.object(sys, "stdin", FakeStdin(tty)):
                yield prompts
            test.assertEqual(list(it), [], "not every answer was used")

        return cm()
