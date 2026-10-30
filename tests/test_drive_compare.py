# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
Unit tests for drive-compare.py: profiles and configuration, the comparison
rules, the ignore file, trash hints, the interactive review and a few end to
end runs against a fake proton-drive CLI.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Every test runs with HOME pointing to a temporary folder, and the modules
are loaded fresh inside it, so nothing touches the real ~/drive-sync-logs,
~/.config/proton-backup or the Proton account.
"""

import configparser
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_CLI = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Fake proton-drive. $FAKE_STATE is a JSON file:
    {"drive": {"/my-files/a.txt": "content" or ["content", "2024-01-01T00:00:00Z"]},
     "trash": [{"type": "file", "name": "x", "content": "...", "when": "2026-10-12T10:00:00Z"},
               {"type": "folder", "name": "Old", "when": "..."}],
     "trash_fails": false}"""
    import hashlib, json, os, sys
    st = json.load(open(os.environ["FAKE_STATE"], encoding="utf-8"))
    args = sys.argv[1:]

    def node(name, content, mtime):
        b = content.encode()
        return {"name": {"ok": True, "value": name}, "type": "file", "uid": "f-" + name,
                "activeRevision": {"claimedSize": len(b), "claimedModificationTime": mtime,
                                   "claimedDigests": {"sha1": hashlib.sha1(b).hexdigest()}}}

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
            content, mtime = (v, "2024-01-01T00:00:00Z") if isinstance(v, str) else v
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


class DriveCompareTestCase(unittest.TestCase):
    """Loads protonlib and drive-compare.py fresh, with HOME in a temp folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        env = mock.patch.dict(os.environ, {"HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        saved = sys.modules.get("protonlib")
        self.addCleanup(self._restore_protonlib, saved)

        spec = importlib.util.spec_from_file_location("protonlib", os.path.join(REPO, "protonlib.py"))
        self.plib = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plib)
        sys.modules["protonlib"] = self.plib
        spec = importlib.util.spec_from_file_location(
            "drive_compare", os.path.join(REPO, "drive-compare.py"))
        self.dc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dc)

        # sanity: everything the module will write lives under the temp HOME
        self.assertTrue(self.dc.CONFIG_DIR.startswith(self.home))
        self.assertTrue(self.dc.CLOUD_STORAGE.startswith(self.home))
        self.assertTrue(self.plib.LOG_DIR.startswith(self.home))
        self.config = os.path.join(self.home, ".config", "proton-backup", "config.ini")

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _restore_protonlib(saved):
        if saved is None:
            sys.modules.pop("protonlib", None)
        else:
            sys.modules["protonlib"] = saved

    # -- helpers ------------------------------------------------------------

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

    def args(self, *argv):
        with mock.patch.object(sys, "argv", ["drive-compare.py", *argv]):
            return self.dc.parse_args()

    def resolve(self, *argv):
        return self.dc.resolve_settings(self.args(*argv, "--config", self.config))

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


# ---------------------------------------------------------------------------
# 1. Resolving the settings of a run
# ---------------------------------------------------------------------------

class ResolveSettingsTest(DriveCompareTestCase):

    def setUp(self):
        super().setUp()
        self.folder = self.mkdir("Dropbox")

    def test_profile_name_loads_its_section(self):
        self.write_config(f"""
            [dropbox]
            folder = {self.folder}
            winner = ask
            """)
        s = self.resolve("dropbox")
        self.assertEqual(s["name"], "dropbox")
        self.assertEqual(s["folder"], self.folder)
        self.assertEqual(s["winner"], "ask")
        self.assertIn("profile [dropbox]", s["where"])

    def test_unset_profile_keys_take_the_built_in_defaults(self):
        self.write_config(f"""
            [dropbox]
            folder = {self.folder}
            """)
        s = self.resolve("dropbox")
        self.assertEqual(s["proton_root"], "/my-files")
        self.assertEqual(s["compare_by"], "content")
        self.assertEqual(s["winner"], "proton")
        self.assertEqual(s["moved"], "hide")
        self.assertEqual(s["proton_only"], "hide")
        self.assertIs(s["copy_script"], False)
        self.assertEqual(s["review_max"], 20)
        self.assertEqual(s["exclude"], set())

    def test_profile_inherits_from_default_section(self):
        self.write_config(f"""
            [DEFAULT]
            winner = newer
            moved = list

            [dropbox]
            folder = {self.folder}
            moved = hide
            """)
        s = self.resolve("dropbox")
        self.assertEqual(s["winner"], "newer")
        self.assertEqual(s["moved"], "hide")

    def test_folder_path_uses_defaults_and_default_section(self):
        self.write_config("""
            [DEFAULT]
            winner = ask
            """)
        s = self.resolve(self.folder)
        self.assertEqual(s["name"], "folder")
        self.assertEqual(s["file_name"], "folder")
        self.assertEqual(s["winner"], "ask")
        self.assertEqual(s["compare_by"], "content")
        self.assertEqual(s["where"], "defaults and command line")

    def test_folder_path_works_without_a_config_file(self):
        self.assertFalse(os.path.exists(self.config))
        s = self.resolve(self.folder)
        self.assertEqual(s["name"], "folder")
        self.assertEqual(s["winner"], "proton")

    def test_path_like_targets_are_never_profiles(self):
        # a profile and a folder with the same name: "docs" is the profile,
        # "./docs" the folder
        docs = self.mkdir("cwd", "docs")
        self.write_config(f"""
            [docs]
            folder = {self.folder}
            """)
        cwd = os.getcwd()
        os.chdir(os.path.dirname(docs))
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(self.resolve("docs")["name"], "docs")
        s = self.resolve("./docs")
        self.assertEqual(s["name"], "folder")
        self.assertEqual(os.path.realpath(s["folder"]), docs)

    def test_tilde_target_is_a_path(self):
        self.assertExits(lambda: self.resolve("~nobody-zz-test/Dropbox"), "doesn't exist")

    def test_tilde_target_expands_to_home(self):
        dbx = os.path.join(self.home, "Dropbox")
        os.makedirs(dbx)
        s = self.resolve("~/Dropbox")
        self.assertEqual(s["folder"], dbx)

    def test_unknown_profile_lists_profiles_and_suggests_setup(self):
        self.write_config(f"""
            [dropbox]
            folder = {self.folder}

            [ssd]
            folder = {self.folder}
            """)
        self.assertExits(lambda: self.resolve("nope"),
                         "'nope' is neither a profile", "dropbox, ssd",
                         "./drive-compare.py --setup nope")

    def test_unknown_profile_without_config_says_none_yet(self):
        self.assertExits(lambda: self.resolve("nope"), "profiles: none yet")

    def test_profile_without_folder_is_an_error(self):
        self.write_config("""
            [empty]
            winner = ask
            """)
        self.assertExits(lambda: self.resolve("empty"),
                         "profile [empty]", "has no folder", "--setup empty")

    def test_profile_folder_that_does_not_exist_is_an_error(self):
        missing = os.path.join(self.tmp, "Unplugged")
        self.write_config(f"""
            [disk]
            folder = {missing}
            """)
        self.assertExits(lambda: self.resolve("disk"), missing, "doesn't exist", "profile [disk]")

    def test_folder_path_that_does_not_exist_is_an_error(self):
        self.assertExits(lambda: self.resolve(os.path.join(self.tmp, "nowhere")),
                         "doesn't exist")

    def test_command_line_overrides_the_profile(self):
        pf = self.mkdir("PF")
        self.write_config(f"""
            [dropbox]
            folder = {self.folder}
            proton_root = /my-files
            exclude = A
            compare_by = content
            winner = proton
            moved = hide
            proton_only = hide
            copy_script = no
            review_max = 20
            """)
        s = self.resolve("dropbox", "--root", "/my-files/Work", "--exclude", "B, C",
                         "--compare-by", "size", "--winner", "folder", "--moved", "list",
                         "--proton-only", "list", "--copy-script", "--review-max", "3",
                         "--ignore-file", "~/my-ignore.txt", "--proton-folder", pf)
        self.assertEqual(s["proton_root"], "/my-files/Work")
        self.assertEqual(s["exclude"], {"B", "C"})
        self.assertEqual(s["compare_by"], "size")
        self.assertEqual(s["winner"], "folder")
        self.assertEqual(s["moved"], "list")
        self.assertEqual(s["proton_only"], "list")
        self.assertIs(s["copy_script"], True)
        self.assertEqual(s["review_max"], 3)
        self.assertEqual(s["ignore_file"], os.path.join(self.home, "my-ignore.txt"))
        self.assertEqual(s["proton_folder"], pf)

    def test_legacy_sha1_option_forces_content_comparison(self):
        self.write_config(f"""
            [dropbox]
            folder = {self.folder}
            compare_by = size
            """)
        self.assertEqual(self.resolve("dropbox", "--sha1")["compare_by"], "content")

    def test_legacy_exclude_dirs_option_is_still_accepted(self):
        s = self.resolve(self.folder, "--exclude-dirs", "Camera Uploads,Photos")
        self.assertEqual(s["exclude"], {"Camera Uploads", "Photos"})

    def test_invalid_choice_names_profile_file_and_allowed_values(self):
        self.write_config(f"""
            [dropbox]
            folder = {self.folder}
            winner = maybe
            """)
        self.assertExits(lambda: self.resolve("dropbox"),
                         f"profile [dropbox] in {self.config}", "winner = 'maybe'",
                         "proton, folder, newer, ask")

    def test_every_choice_setting_is_validated(self):
        for key in ("compare_by", "moved", "proton_only", "copy_script"):
            with self.subTest(key=key):
                self.write_config(f"""
                    [p]
                    folder = {self.folder}
                    {key} = bogus
                    """)
                self.assertExits(lambda: self.resolve("p"), f"{key} = 'bogus'")

    def test_review_max_must_be_a_non_negative_number(self):
        for value in ("abc", "-1", "2.5"):
            with self.subTest(value=value):
                self.write_config(f"""
                    [p]
                    folder = {self.folder}
                    review_max = {value}
                    """)
                self.assertExits(lambda: self.resolve("p"),
                                 "profile [p]", "review_max must be a number, 0 or more")

    def test_negative_review_max_on_the_command_line_is_an_error(self):
        self.assertExits(lambda: self.resolve(self.folder, "--review-max", "-1"),
                         "review_max must be a number")

    def test_review_max_zero_is_allowed(self):
        self.assertEqual(self.resolve(self.folder, "--review-max", "0")["review_max"], 0)

    def test_inline_comments_are_stripped_from_values(self):
        self.write_config(f"""
            [p]
            folder = {self.folder}   ; the cloud folder
            winner = ask             # decide by hand
            exclude = Skip, Photos   ; not these
            """)
        s = self.resolve("p")
        self.assertEqual(s["folder"], self.folder)
        self.assertEqual(s["winner"], "ask")
        self.assertEqual(s["exclude"], {"Skip", "Photos"})

    def test_percent_in_paths_is_taken_literally(self):
        odd = self.mkdir("100%done")
        self.write_config(f"""
            [p]
            folder = {odd}
            """)
        self.assertEqual(self.resolve("p")["folder"], odd)

    def test_tilde_in_profile_folder_is_expanded(self):
        dbx = os.path.join(self.home, "Dropbox")
        os.makedirs(dbx)
        self.write_config("""
            [p]
            folder = ~/Dropbox/
            """)
        self.assertEqual(self.resolve("p")["folder"], dbx)

    def test_default_ignore_file_is_named_after_the_profile(self):
        self.write_config(f"""
            [ssd]
            folder = {self.folder}
            """)
        self.assertEqual(self.resolve("ssd")["ignore_file"],
                         os.path.join(self.home, ".config", "proton-backup", "ignore-ssd.txt"))
        self.assertEqual(self.resolve(self.folder)["ignore_file"],
                         os.path.join(self.home, ".config", "proton-backup", "ignore-folder.txt"))

    def test_ignore_file_setting_overrides_the_default(self):
        self.write_config(f"""
            [ssd]
            folder = {self.folder}
            ignore_file = ~/lists/ssd-ignore.txt
            """)
        self.assertEqual(self.resolve("ssd")["ignore_file"],
                         os.path.join(self.home, "lists", "ssd-ignore.txt"))

    def test_hand_written_profile_name_is_sanitized_for_file_names(self):
        self.write_config(f"""
            [my disk!]
            folder = {self.folder}
            """)
        s = self.resolve("my disk!")
        self.assertEqual(s["name"], "my disk!")
        self.assertEqual(s["file_name"], "my_disk")
        self.assertTrue(s["ignore_file"].endswith("ignore-my_disk.txt"))

    def test_missing_target_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.args()
        self.assertEqual(cm.exception.code, 2)

    def test_unreadable_config_is_reported(self):
        self.write_config("this is not ini\n[ok]\n")
        self.assertExits(lambda: self.resolve(self.folder), "can't read", self.config)


# ---------------------------------------------------------------------------
# 2. Writing the configuration
# ---------------------------------------------------------------------------

class ConfigFileTest(DriveCompareTestCase):

    def make_cfg(self):
        cfg = self.dc.read_config(os.path.join(self.tmp, "none.ini"))
        cfg["DEFAULT"]["compare_by"] = "size"
        cfg.add_section("dropbox")
        cfg.set("dropbox", "folder", "~/Dropbox")
        cfg.set("dropbox", "winner", "ask")
        cfg.set("dropbox", "colour", "blue")    # unknown key, written by hand
        cfg.add_section("ssd")
        cfg.set("ssd", "folder", "/Volumes/Disk/100%")
        return cfg

    def test_round_trip_keeps_every_value(self):
        self.dc.write_config(self.config, self.make_cfg())
        back = self.dc.read_config(self.config)
        self.assertEqual(back.sections(), ["dropbox", "ssd"])
        self.assertEqual(back.defaults()["compare_by"], "size")
        self.assertEqual(back.get("dropbox", "folder"), "~/Dropbox")
        self.assertEqual(back.get("dropbox", "winner"), "ask")
        self.assertEqual(back.get("dropbox", "colour"), "blue")
        self.assertEqual(back.get("ssd", "folder"), "/Volumes/Disk/100%")
        self.assertEqual(back.get("ssd", "compare_by"), "size")    # inherited

    def test_every_known_setting_is_preceded_by_its_explanation(self):
        text = self.dc.render_config(self.make_cfg())
        lines = text.splitlines()
        for key in ("folder", "winner", "compare_by"):
            i = next(n for n, l in enumerate(lines) if l.startswith(f"{key} = "))
            self.assertEqual(lines[i - 1], f"; {self.dc.HELP[key]}")
        self.assertTrue(lines[0].startswith("# drive-compare.py profiles"))

    def test_default_section_is_written_first(self):
        text = self.dc.render_config(self.make_cfg())
        self.assertLess(text.index("[DEFAULT]"), text.index("[dropbox]"))

    def test_profiles_keep_only_their_own_keys(self):
        text = self.dc.render_config(self.make_cfg())
        dropbox = text.split("[dropbox]")[1].split("[ssd]")[0]
        self.assertNotIn("compare_by", dropbox)

    def test_unknown_keys_are_kept_after_the_known_ones(self):
        text = self.dc.render_config(self.make_cfg())
        dropbox = text.split("[dropbox]")[1].split("[ssd]")[0]
        self.assertIn("colour = blue", dropbox)
        self.assertLess(dropbox.index("winner = "), dropbox.index("colour = "))

    def test_write_creates_the_config_folder(self):
        self.assertFalse(os.path.exists(os.path.dirname(self.config)))
        self.dc.write_config(self.config, self.make_cfg())
        self.assertTrue(os.path.isfile(self.config))
        self.assertEqual(os.listdir(os.path.dirname(self.config)), ["config.ini"])

    def test_write_through_a_symlink_keeps_the_link(self):
        # e.g. config.ini linked to a file kept in a private repository
        target = self.write(os.path.join(self.tmp, "private-repo", "config.ini"), "[old]\n")
        os.makedirs(os.path.dirname(self.config))
        os.symlink(target, self.config)
        self.dc.write_config(self.config, self.make_cfg())
        self.assertTrue(os.path.islink(self.config))
        self.assertEqual(os.path.realpath(self.config), target)
        self.assertEqual(self.dc.read_config(target).sections(), ["dropbox", "ssd"])
        self.assertEqual(os.listdir(os.path.dirname(target)), ["config.ini"])

    def test_failed_write_leaves_the_old_file_untouched(self):
        self.write_config("[old]\nfolder = /x\n")
        with mock.patch.object(self.dc.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.dc.write_config(self.config, self.make_cfg())
        with open(self.config, encoding="utf-8") as f:
            self.assertEqual(f.read(), "[old]\nfolder = /x\n")

    def test_example_config_in_the_repo_is_valid(self):
        example = os.path.join(REPO, "config.example.ini")
        if not os.path.exists(example):
            self.skipTest("config.example.ini not in the repository yet")
        cfg = self.dc.read_config(example)
        for name in cfg.sections():
            s = dict(self.dc.DEFAULTS)
            s.update(cfg.items(name))
            with contextlib.redirect_stderr(io.StringIO()):
                self.dc.check_settings(s, f"[{name}] in config.example.ini")


# ---------------------------------------------------------------------------
# 3. --setup
# ---------------------------------------------------------------------------

class SetupTest(DriveCompareTestCase):

    QUESTIONS = ("folder", "proton_root", "exclude", "compare_by", "winner",
                 "moved", "proton_only", "copy_script", "review_max")

    def setUp(self):
        super().setUp()
        self.folder = self.mkdir("Dropbox")
        self.mkdir("Dropbox", "Photos")

    def run_setup(self, answers, *argv, tty=True):
        """Run --setup answering in order; returns (exit value, output)."""
        a = self.args("--setup", *argv, "--config", self.config)
        it = iter(answers)

        def fake_input(prompt=""):
            try:
                ans = next(it)
            except StopIteration:
                self.fail(f"unexpected question: {prompt!r}")
            if isinstance(ans, BaseException):
                raise ans
            return ans

        out = io.StringIO()
        with mock.patch("builtins.input", fake_input), \
                mock.patch.object(sys, "stdin", FakeStdin(tty)), \
                contextlib.redirect_stdout(out):
            rv = self.dc.setup(a)
        self.assertEqual(list(it), [], "not every answer was used")
        return rv, out.getvalue()

    def saved(self):
        return self.dc.read_config(self.config)

    def test_new_profile_with_explicit_answers(self):
        rv, out = self.run_setup([self.folder, "/my-files/Dropbox", "Photos", "size",
                                  "newer", "list", "list", "yes", "5"], "dropbox")
        self.assertEqual(rv, 0)
        cfg = self.saved()
        self.assertEqual(dict(cfg.items("dropbox")), {
            "folder": self.folder, "proton_root": "/my-files/Dropbox", "exclude": "Photos",
            "compare_by": "size", "winner": "newer", "moved": "list",
            "proton_only": "list", "copy_script": "yes", "review_max": "5"})
        self.assertIn("folders in it: Photos", out)
        self.assertIn("./drive-compare.py dropbox", out)

    def test_enter_keeps_the_defaults(self):
        self.run_setup([self.folder] + [""] * 8, "dropbox")
        p = dict(self.saved().items("dropbox"))
        self.assertEqual(p["proton_root"], "/my-files")
        self.assertEqual(p["exclude"], "")
        self.assertEqual(p["compare_by"], "content")
        self.assertEqual(p["winner"], "proton")
        self.assertEqual(p["copy_script"], "no")
        self.assertEqual(p["review_max"], "20")

    def test_saved_profile_resolves(self):
        self.run_setup([self.folder, "", "Photos", "", "ask", "", "", "", ""], "dropbox")
        s = self.resolve("dropbox")
        self.assertEqual(s["folder"], self.folder)
        self.assertEqual(s["exclude"], {"Photos"})
        self.assertEqual(s["winner"], "ask")

    def test_invalid_choice_is_asked_again(self):
        _, out = self.run_setup([self.folder, "", "", "sha256", "content",
                                 "always", "folder", "", "", "", ""], "dropbox")
        self.assertEqual(self.saved().get("dropbox", "winner"), "folder")
        self.assertIn("answer one of: content, size", out)
        self.assertIn("answer one of: proton, folder, newer, ask", out)

    def test_review_max_must_be_digits(self):
        _, out = self.run_setup([self.folder, "", "", "", "", "", "", "", "ten", "-3", "7"],
                                "dropbox")
        self.assertEqual(self.saved().get("dropbox", "review_max"), "7")
        self.assertEqual(out.count("a number, 0 or more"), 2)

    def test_invalid_profile_name_is_asked_again(self):
        _, out = self.run_setup(["also bad!", "ssd", self.folder] + [""] * 8, "bad name")
        self.assertEqual(self.saved().sections(), ["ssd"])
        self.assertEqual(out.count("letters, digits"), 2)

    def test_profile_name_is_asked_when_missing(self):
        self.run_setup(["ssd", self.folder] + [""] * 8)
        self.assertEqual(self.saved().sections(), ["ssd"])

    def test_missing_folder_can_be_kept_anyway(self):
        unplugged = "/Volumes/NotPluggedIn/Backup"
        _, out = self.run_setup([unplugged, "yes"] + [""] * 8, "ssd")
        self.assertEqual(self.saved().get("ssd", "folder"), unplugged)
        self.assertIn("doesn't exist", out)

    def test_missing_folder_is_asked_again_when_not_kept(self):
        self.run_setup(["/Volumes/NotPluggedIn", "", self.folder] + [""] * 8, "ssd")
        self.assertEqual(self.saved().get("ssd", "folder"), self.folder)

    def test_changing_a_profile_keeps_the_others(self):
        self.write_config(f"""
            [dropbox]
            folder = {self.folder}
            winner = ask

            [ssd]
            folder = /Volumes/Disk
            moved = list
            """)
        _, out = self.run_setup(["", "", "", "", "", "", "", "", ""], "dropbox")
        cfg = self.saved()
        self.assertIn("Changing profile [dropbox]", out)
        self.assertEqual(cfg.get("dropbox", "winner"), "ask")       # Enter kept it
        self.assertEqual(cfg.get("dropbox", "folder"), self.folder)
        self.assertEqual(dict(cfg.items("ssd")), {"folder": "/Volumes/Disk", "moved": "list"})

    def test_current_values_are_offered_as_defaults(self):
        self.write_config(f"""
            [DEFAULT]
            winner = newer

            [dropbox]
            folder = {self.folder}
            exclude = Photos
            """)
        prompts = []
        a = self.args("--setup", "dropbox", "--config", self.config)
        with mock.patch("builtins.input", lambda p="": prompts.append(p) or ""), \
                mock.patch.object(sys, "stdin", FakeStdin(True)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.dc.setup(a)
        self.assertIn(f"({self.folder})", prompts[0])
        self.assertIn("(Photos)", prompts[2])
        self.assertIn("(newer)", prompts[4])

    def test_refuses_without_a_terminal(self):
        self.assertExits(lambda: self.run_setup([], "dropbox", tty=False),
                         "run it in a terminal")
        self.assertFalse(os.path.exists(self.config))

    def test_ctrl_c_writes_nothing(self):
        self.assertExits(lambda: self.run_setup([self.folder, "", KeyboardInterrupt()], "dropbox"),
                         "nothing written")
        self.assertFalse(os.path.exists(self.config))

    def test_end_of_input_in_a_question_writes_nothing(self):
        self.assertExits(lambda: self.run_setup([self.folder, "", "", EOFError()], "dropbox"),
                         "nothing written")
        self.assertFalse(os.path.exists(self.config))

    def test_ctrl_c_on_an_existing_config_leaves_it_untouched(self):
        self.write_config(f"[keep]\nfolder = {self.folder}\n")
        before = read(self.config)
        self.assertExits(lambda: self.run_setup([KeyboardInterrupt()], "keep"), "nothing written")
        self.assertEqual(read(self.config), before)

    # the profile-name prompt calls input() directly, outside ask()
    def test_end_of_input_at_the_profile_name_exits_cleanly(self):
        self.assertExits(lambda: self.run_setup([EOFError()]), "nothing written")


# ---------------------------------------------------------------------------
# 4. Comparison rules
# ---------------------------------------------------------------------------

class CompareTest(DriveCompareTestCase):
    """compare() on hand-built inventories: local entries are
    (size, mtime, full path, dataless), Proton entries (size, mtime, sha1)."""

    T0 = epoch("2024-01-01")

    def setUp(self):
        super().setUp()
        self.root = self.mkdir("Src")
        self.local, self.remote = {}, {}
        self.ignore = os.path.join(self.tmp, "ignore.txt")

    def here(self, rel, text, mtime=None, dataless=False, size=None):
        """A file in the folder. dataless=True: an online-only placeholder
        (no content on disk, size as reported by the cloud)."""
        full = os.path.join(self.root, rel)
        self.write(full, "" if dataless else text)
        size = len(text.encode()) if size is None else size
        self.local[unicodedata.normalize("NFC", rel)] = (
            size, int(mtime or self.T0), full, True if dataless else False)

    def placeholder(self, rel):
        """A 0-byte, 0-block file: what Dropbox leaves for an online-only file."""
        full = os.path.join(self.root, rel)
        self.write(full, "")
        self.local[rel] = (0, int(self.T0), full, None)

    def proton(self, rel, text, mtime=None, sha="auto"):
        self.remote[rel] = (len(text.encode()), int(mtime or self.T0),
                            sha1(text) if sha == "auto" else sha)

    def run_compare(self, include_online=False, **settings):
        s = {"compare_by": "content", "winner": "proton", "ignore_file": self.ignore}
        s.update(settings)
        with contextlib.redirect_stderr(io.StringIO()):
            return self.dc.compare(s, self.local, self.remote, include_online)

    # identical

    def test_same_content_is_identical(self):
        self.here("a.txt", "alpha")
        self.proton("a.txt", "alpha")
        res = self.run_compare()
        self.assertEqual(res.identical, ["a.txt"])
        self.assertEqual(res.hashed, 1)

    def test_same_size_different_content_is_a_different_version(self):
        self.here("a.txt", "alpha")
        self.proton("a.txt", "ALPHA")
        res = self.run_compare(winner="ask")
        self.assertEqual(res.identical, [])
        self.assertEqual(res.decide_diff, [("a.txt", "different content")])

    def test_compare_by_size_trusts_name_and_size(self):
        self.here("a.txt", "alpha")
        self.proton("a.txt", "ALPHA")
        res = self.run_compare(compare_by="size")
        self.assertEqual(res.identical, ["a.txt"])
        self.assertEqual(res.hashed, 0)

    def test_compare_by_size_still_sees_a_size_change(self):
        self.here("a.txt", "alpha")
        self.proton("a.txt", "alpha!")
        res = self.run_compare(compare_by="size", winner="ask")
        self.assertEqual([r for r, _ in res.decide_diff], ["a.txt"])

    def test_proton_file_without_sha1_cannot_be_verified(self):
        self.here("a.txt", "alpha")
        self.proton("a.txt", "alpha", sha="-")
        res = self.run_compare()
        self.assertEqual(res.no_hash, ["a.txt"])
        self.assertEqual(res.identical, [])

    # online-only placeholders at the same path

    def test_dataless_file_at_same_path_is_not_verified(self):
        self.here("a.txt", "alpha", dataless=True)
        self.proton("a.txt", "alpha")
        res = self.run_compare()
        self.assertEqual(res.online, ["a.txt"])
        self.assertEqual(res.hashed, 0)

    def test_zero_byte_placeholder_at_same_path_is_not_verified(self):
        self.placeholder("a.txt")
        self.proton("a.txt", "alpha")
        res = self.run_compare()
        self.assertEqual(res.online, ["a.txt"])

    def test_really_empty_file_on_both_sides_is_identical(self):
        self.placeholder("empty.txt")
        self.proton("empty.txt", "")
        res = self.run_compare()
        self.assertEqual(res.identical, ["empty.txt"])

    def test_include_online_reads_placeholders(self):
        # with --include-online the file is read (in real life this forces
        # the download); here its content is already on disk
        full = self.write(os.path.join(self.root, "a.txt"), "alpha")
        self.local["a.txt"] = (5, int(self.T0), full, True)
        self.proton("a.txt", "alpha")
        res = self.run_compare(include_online=True)
        self.assertEqual(res.online, [])
        self.assertEqual(res.identical, ["a.txt"])

    # winner

    def setup_versions(self):
        self.here("newer-here.txt", "new version", mtime=epoch("2025-06-01"))
        self.proton("newer-here.txt", "old", mtime=epoch("2024-01-01"))
        self.here("newer-on-proton.txt", "stale", mtime=epoch("2023-01-01"))
        self.proton("newer-on-proton.txt", "fresh", mtime=epoch("2024-01-01"))
        self.here("same-date.txt", "mine", mtime=epoch("2024-01-01") + 1)
        self.proton("same-date.txt", "theirs", mtime=epoch("2024-01-01"))

    def test_winner_proton_keeps_every_proton_version(self):
        self.setup_versions()
        res = self.run_compare(winner="proton")
        self.assertEqual(sorted(res.proton_kept),
                         ["newer-here.txt", "newer-on-proton.txt", "same-date.txt"])
        self.assertEqual(res.replace, [])
        self.assertEqual(res.decide_diff, [])

    def test_winner_folder_replaces_every_proton_version(self):
        self.setup_versions()
        res = self.run_compare(winner="folder")
        self.assertEqual(sorted(res.replace),
                         ["newer-here.txt", "newer-on-proton.txt", "same-date.txt"])
        self.assertEqual(res.proton_kept, [])

    def test_winner_newer_picks_by_date_and_asks_on_a_tie(self):
        self.setup_versions()
        res = self.run_compare(winner="newer")
        self.assertEqual(res.replace, ["newer-here.txt"])
        self.assertEqual(res.proton_kept, ["newer-on-proton.txt"])
        self.assertEqual(res.decide_diff, [("same-date.txt", "same date, different content")])

    def test_winner_newer_tolerance_is_two_seconds(self):
        self.here("a.txt", "mine", mtime=self.T0 + 3)
        self.proton("a.txt", "yours", mtime=self.T0)
        self.assertEqual(self.run_compare(winner="newer").replace, ["a.txt"])

    def test_winner_ask_lists_every_different_version(self):
        self.setup_versions()
        res = self.run_compare(winner="ask")
        self.assertEqual(sorted(r for r, _ in res.decide_diff),
                         ["newer-here.txt", "newer-on-proton.txt", "same-date.txt"])

    # moved or renamed

    def test_file_on_disk_found_elsewhere_by_content(self):
        self.here("Old/scan.pdf", "scanned")
        self.proton("New/Scan 2019.pdf", "scanned")
        res = self.run_compare()
        self.assertEqual(res.moved, [("Old/scan.pdf", "New/Scan 2019.pdf", "same content")])
        self.assertEqual(res.decide_missing, [])

    def test_same_name_but_different_content_is_not_moved(self):
        self.here("Old/report.pdf", "version A")
        self.proton("New/report.pdf", "version B")
        res = self.run_compare()
        self.assertEqual(res.moved, [])
        self.assertEqual(res.decide_missing, ["Old/report.pdf"])

    def test_dataless_file_found_by_name_and_size(self):
        self.here("Old/doc.pdf", "document", dataless=True)
        self.proton("New/doc.pdf", "document")
        res = self.run_compare()
        self.assertEqual(res.moved, [("Old/doc.pdf", "New/doc.pdf", "same name and size")])
        self.assertEqual(res.hashed, 0)

    def test_dataless_file_with_another_size_is_missing(self):
        self.here("Old/doc.pdf", "document", dataless=True)
        self.proton("New/doc.pdf", "document v2")
        self.assertEqual(self.run_compare().decide_missing, ["Old/doc.pdf"])

    def test_compare_by_size_finds_moved_files_by_name_and_size(self):
        self.here("Old/doc.pdf", "document")
        self.proton("New/DOC.pdf", "DOCUMENT")
        res = self.run_compare(compare_by="size")
        self.assertEqual(res.moved, [("Old/doc.pdf", "New/DOC.pdf", "same name and size")])
        self.assertEqual(res.hashed, 0)

    def test_zero_byte_placeholder_found_by_name_only(self):
        self.placeholder("Docs/Report 2023.pdf")
        self.proton("Docs/Archive/2023/report 2023.pdf", "pdf bytes")
        res = self.run_compare()
        self.assertEqual(res.moved, [("Docs/Report 2023.pdf",
                                      "Docs/Archive/2023/report 2023.pdf",
                                      "same name, content not verified")])

    def test_zero_byte_placeholder_with_no_namesake_is_missing(self):
        self.placeholder("Docs/unique.pdf")
        self.proton("Other/else.pdf", "x")
        self.assertEqual(self.run_compare().decide_missing, ["Docs/unique.pdf"])

    def test_moved_targets_are_not_counted_as_only_on_proton(self):
        self.here("Old/a.txt", "alpha")
        self.proton("New/a.txt", "alpha")
        self.proton("New/only-there.txt", "zeta")
        res = self.run_compare()
        self.assertEqual(res.proton_only, ["New/only-there.txt"])

    # ignore file

    def write_ignore(self, *lines):
        self.write(self.ignore, "\n".join(lines) + "\n")

    def test_ignore_exact_path(self):
        self.write_ignore("missing.txt")
        self.here("missing.txt", "x")
        self.here("other.txt", "y")
        res = self.run_compare()
        self.assertEqual(res.ignored, ["missing.txt"])
        self.assertEqual(res.decide_missing, ["other.txt"])

    def test_ignore_folder_covers_everything_under_it(self):
        self.write_ignore("Old Stuff/")
        self.here("Old Stuff/a.txt", "a")
        self.here("Old Stuff/deep/b.txt", "b")
        self.here("Old Stuffing.txt", "c")
        res = self.run_compare()
        self.assertEqual(sorted(res.ignored), ["Old Stuff/a.txt", "Old Stuff/deep/b.txt"])
        self.assertEqual(res.decide_missing, ["Old Stuffing.txt"])

    def test_ignore_wildcards_match_across_folders(self):
        self.write_ignore("*.tmp", "Lavoro/20?? archive")
        self.here("a/b/c.tmp", "t")
        self.here("Lavoro/2019 archive/x.pdf", "x")
        self.here("Lavoro/2019 current/y.pdf", "y")
        res = self.run_compare()
        self.assertEqual(sorted(res.ignored), ["Lavoro/2019 archive/x.pdf", "a/b/c.tmp"])
        self.assertEqual(res.decide_missing, ["Lavoro/2019 current/y.pdf"])

    def test_ignore_is_case_insensitive(self):
        self.write_ignore("docs/HEALTH")
        self.here("Docs/Health/x.pdf", "x")
        self.assertEqual(self.run_compare().ignored, ["Docs/Health/x.pdf"])

    def test_ignore_matches_nfd_and_nfc_names(self):
        self.write_ignore(unicodedata.normalize("NFD", "Maternità"))
        self.here(unicodedata.normalize("NFC", "Maternità") + "/scan.pdf", "s")
        self.assertEqual(len(self.run_compare().ignored), 1)

    def test_ignore_skips_comments_and_blank_lines(self):
        self.write_ignore("# missing.txt", "", "   ")
        self.here("missing.txt", "x")
        res = self.run_compare()
        self.assertEqual(res.ignored, [])
        self.assertEqual(res.decide_missing, ["missing.txt"])

    def test_missing_ignore_file_means_nothing_ignored(self):
        self.here("missing.txt", "x")
        self.assertEqual(self.run_compare().decide_missing, ["missing.txt"])

    def test_ignore_applies_to_different_versions_to_decide(self):
        self.write_ignore("Docs/cv.pdf")
        self.here("Docs/cv.pdf", "mine")
        self.proton("Docs/cv.pdf", "theirs")
        res = self.run_compare(winner="ask")
        self.assertEqual(res.ignored, ["Docs/cv.pdf"])
        self.assertEqual(res.decide_diff, [])

    def test_ignore_applies_to_same_date_ties_with_winner_newer(self):
        self.write_ignore("tie.txt")
        self.here("tie.txt", "mine", mtime=self.T0)
        self.proton("tie.txt", "theirs", mtime=self.T0)
        res = self.run_compare(winner="newer")
        self.assertEqual(res.ignored, ["tie.txt"])
        self.assertEqual(res.decide_diff, [])

    # noise

    def test_noise_in_the_folder_is_only_counted(self):
        for rel in ("Report.pages/Data/img.png", "Plan.lh3d/Meshes/a.mesh", "Icon\r",
                    "Sub/Icon\r", ".dropbox", "Backup del computer.dbx-backup",
                    "App Passwords.dbx-passwords", "Docs/~$draft.docx"):
            self.here(rel, "n")
        res = self.run_compare()
        self.assertEqual(res.decide_missing, [])
        # four categories (packages, Icon, Dropbox, Office locks); the exact
        # labels are wording, not behaviour
        self.assertEqual(sorted(len(v) for v in res.noise.values()), [1, 2, 2, 3])

    def test_noise_on_proton_is_only_counted(self):
        for rel in ("Report.pages.pkgf", "Docs/~$draft.docx", "Icon\r", "real.txt"):
            self.proton(rel, "n")
        res = self.run_compare()
        self.assertEqual(res.proton_only, ["real.txt"])
        self.assertEqual(sum(len(v) for v in res.proton_noise.values()), 3)

    def test_noise_check_comes_before_the_ignore_file(self):
        self.write_ignore("Report.pages")
        self.here("Report.pages/Data/a.png", "a")
        res = self.run_compare()
        self.assertEqual(res.ignored, [])
        self.assertEqual(sum(len(v) for v in res.noise.values()), 1)


class ScanBothSidesTest(DriveCompareTestCase):

    def test_scan_folder_skips_excluded_top_level_folders_only(self):
        root = self.mkdir("Src")
        self.write(os.path.join(root, "Camera Uploads", "a.jpg"), "a")
        self.write(os.path.join(root, "Docs", "Camera Uploads", "b.jpg"), "b")
        self.write(os.path.join(root, "Docs", "c.txt"), "c")
        with contextlib.redirect_stderr(io.StringIO()):
            files = self.dc.scan_folder(root, {"Camera Uploads"})
        self.assertEqual(sorted(files), ["Docs/Camera Uploads/b.jpg", "Docs/c.txt"])

    def test_scan_folder_skips_junk_and_normalizes_names(self):
        root = self.mkdir("Src")
        nfd = unicodedata.normalize("NFD", "Caffè")
        for rel in (".DS_Store", "._x", "~$lock.docx", ".Trashes/t", f"{nfd}/a.txt", "empty"):
            self.write(os.path.join(root, rel), "" if rel == "empty" else "z")
        with contextlib.redirect_stderr(io.StringIO()):
            files = self.dc.scan_folder(root, set())
        self.assertEqual(sorted(files), ["Caffè/a.txt", "empty"])
        self.assertIsNone(files["empty"][3])        # 0 bytes, 0 blocks: suspicious
        self.assertIs(files["Caffè/a.txt"][3], False)

    def test_scan_folder_excludes_nfd_folder_names(self):
        root = self.mkdir("Src")
        self.write(os.path.join(root, unicodedata.normalize("NFD", "Società"), "a.txt"), "a")
        with contextlib.redirect_stderr(io.StringIO()):
            files = self.dc.scan_folder(root, {unicodedata.normalize("NFC", "Società")})
        self.assertEqual(files, {})

    def test_load_scan_skips_excluded_top_level_folders(self):
        tsv = self.write(os.path.join(self.tmp, "scan.tsv"), "".join([
            "Camera Uploads/a.jpg\t1\t0\tabc\t-\n",
            "Docs/Camera Uploads/b.jpg\t1\t0\tdef\t-\n",
            "Docs/bad\t1\t0\t-\tUNDECRYPTABLE_NAME\n",
        ]))
        remote, problems = self.dc.load_scan(tsv, {"Camera Uploads"})
        self.assertEqual(sorted(remote), ["Docs/Camera Uploads/b.jpg", "Docs/bad"])
        self.assertEqual(problems, [("Docs/bad", "UNDECRYPTABLE_NAME")])


# ---------------------------------------------------------------------------
# 5. Trash hints
# ---------------------------------------------------------------------------

class TrashHintsTest(DriveCompareTestCase):

    def result(self, missing, hashes=None):
        res = self.dc.Result()
        res.decide_missing = list(missing)
        res.local_sha = dict(hashes or {})
        return res

    def trash(self, by_sha=None, by_name=None):
        return mock.patch.object(self.dc, "read_trash",
                                 return_value=(by_sha or {}, by_name or {}))

    def test_file_in_trash_found_by_content(self):
        res = self.result(["Docs/renamed.pdf"], {"Docs/renamed.pdf": "abc"})
        with self.trash(by_sha={"abc": "2026-10-12"}):
            self.assertTrue(self.dc.add_trash_hints(res, "cli"))
        self.assertEqual(res.hints, {"Docs/renamed.pdf": "in the Proton trash since 2026-10-12"})

    def test_file_in_trash_found_by_name(self):
        res = self.result(["Docs/Old.PDF"])
        with self.trash(by_name={("file", "old.pdf"): "2026-10-01"}):
            self.dc.add_trash_hints(res, "cli")
        self.assertIn("a file with this name is in the Proton trash since 2026-10-01",
                      res.hints["Docs/Old.PDF"])

    def test_file_inside_a_trashed_folder(self):
        res = self.result(["Work/Gone Folder/deep/a.txt"])
        with self.trash(by_name={("folder", "gone folder"): "2026-10-20"}):
            self.dc.add_trash_hints(res, "cli")
        self.assertEqual(res.hints["Work/Gone Folder/deep/a.txt"],
                         "folder 'Gone Folder' is in the Proton trash since 2026-10-20")

    def test_a_folder_name_does_not_match_a_file(self):
        res = self.result(["Docs/Gone"])
        with self.trash(by_name={("folder", "gone"): "2026-10-20"}):
            self.dc.add_trash_hints(res, "cli")
        self.assertEqual(res.hints, {})

    def test_no_files_to_decide_means_no_trash_call(self):
        with mock.patch.object(self.dc, "read_trash") as rt:
            self.assertTrue(self.dc.add_trash_hints(self.result([]), "cli"))
        rt.assert_not_called()

    def test_unreadable_trash_gives_no_hints(self):
        res = self.result(["a.txt"])
        with mock.patch.object(self.dc, "read_trash", return_value=None):
            self.assertFalse(self.dc.add_trash_hints(res, "cli"))
        self.assertEqual(res.hints, {})

    def test_read_trash_parses_the_cli_listing(self):
        listing = [
            {"name": {"ok": True, "value": "Maternità.pdf"}, "type": "file",
             "trashTime": "2026-10-12T10:00:00.000Z",
             "activeRevision": {"claimedDigests": {"sha1": "abc"}}},
            {"name": {"ok": True, "value": "Old Folder"}, "type": "folder",
             "trashTime": "2026-10-20T08:00:00.000Z"},
            {"name": {"ok": False}, "type": "file", "uid": "x"},
        ]
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(listing), stderr="")
        with mock.patch.object(self.plib, "run_cli", return_value=done) as rc:
            by_sha, by_name = self.dc.read_trash("proton-drive")
        self.assertEqual(rc.call_args[0][1], ["filesystem", "list", "/trash", "-j"])
        self.assertEqual(by_sha, {"abc": "2026-10-12"})
        self.assertEqual(by_name, {("file", "maternità.pdf"): "2026-10-12",
                                   ("folder", "old folder"): "2026-10-20"})

    def test_read_trash_returns_none_on_failure(self):
        cases = [
            subprocess.CompletedProcess([], 1, stdout="", stderr="network error"),
            subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"error": 1}', stderr=""),
        ]
        for done in cases:
            with self.subTest(stdout=done.stdout, rc=done.returncode):
                with mock.patch.object(self.plib, "run_cli", return_value=done):
                    self.assertIsNone(self.dc.read_trash("proton-drive"))
        with mock.patch.object(self.plib, "run_cli", side_effect=FileNotFoundError):
            self.assertIsNone(self.dc.read_trash("missing-cli"))


# ---------------------------------------------------------------------------
# 6. Interactive review and the ignore file
# ---------------------------------------------------------------------------

class ReviewTest(DriveCompareTestCase):

    def setUp(self):
        super().setUp()
        root = self.mkdir("Src")
        self.local, self.remote = {}, {}
        for rel in ("m1.txt", "m2.txt", "m3.txt", "d1.txt", "d2.txt"):
            full = self.write(os.path.join(root, rel), rel)
            self.local[rel] = (6, epoch("2024-01-01"), full, False)
        for rel in ("d1.txt", "d2.txt"):
            self.remote[rel] = (9, epoch("2024-02-01"), "x")
        self.s = {"name": "dropbox"}

    def result(self):
        res = self.dc.Result()
        res.decide_missing = ["m1.txt", "m2.txt", "m3.txt"]
        res.decide_diff = [("d1.txt", "different content"), ("d2.txt", "different content")]
        res.hints = {"m2.txt": "in the Proton trash since 2026-10-12"}
        return res

    def run_review(self, answers, res=None):
        res = res or self.result()
        it = iter(answers)

        def fake_input(prompt=""):
            ans = next(it)
            if isinstance(ans, BaseException):
                raise ans
            return ans

        out = io.StringIO()
        with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(out):
            ignored = self.dc.review(self.s, res, self.local, self.remote)
        return res, ignored, out.getvalue()

    def test_every_choice_has_its_effect(self):
        res, ignored, out = self.run_review(["c", "i", "s", "c", "i"])
        self.assertEqual(res.copy_new, ["m1.txt"])
        self.assertEqual(res.replace, ["d1.txt"])
        self.assertEqual(ignored, ["m2.txt", "d2.txt"])
        self.assertEqual(sorted(res.ignored), ["d2.txt", "m2.txt"])
        self.assertEqual(res.decide_missing, ["m3.txt"])
        self.assertEqual(res.decide_diff, [])
        self.assertIn("in the Proton trash since 2026-10-12", out)
        self.assertIn("[5/5] d2.txt", out)

    def test_answers_are_case_and_space_insensitive(self):
        res, ignored, _ = self.run_review([" C ", "I", "s", "S", "s"])
        self.assertEqual(res.copy_new, ["m1.txt"])
        self.assertEqual(ignored, ["m2.txt"])

    def test_invalid_answers_are_asked_again(self):
        res, _, _ = self.run_review(["yes", "", "x", "c", "q"])
        self.assertEqual(res.copy_new, ["m1.txt"])

    def test_q_leaves_the_rest_undecided(self):
        res, ignored, out = self.run_review(["i", "q"])
        self.assertEqual(ignored, ["m1.txt"])
        self.assertEqual(res.decide_missing, ["m2.txt", "m3.txt"])
        self.assertEqual(len(res.decide_diff), 2)
        self.assertIn("Stopped", out)
        self.assertNotIn("[3/5]", out)

    def test_end_of_input_counts_as_q(self):
        res, ignored, _ = self.run_review(["c", EOFError()])
        self.assertEqual(res.copy_new, ["m1.txt"])
        self.assertEqual(ignored, [])
        self.assertEqual(res.decide_missing, ["m2.txt", "m3.txt"])

    def test_ctrl_c_counts_as_q(self):
        res, _, _ = self.run_review([KeyboardInterrupt()])
        self.assertEqual(len(res.decide_missing), 3)

    def test_skip_everything_changes_nothing(self):
        res, ignored, _ = self.run_review(["s"] * 5)
        self.assertEqual(ignored, [])
        self.assertEqual(res.copy_new, [])
        self.assertEqual(res.replace, [])
        self.assertEqual(len(res.decide_missing) + len(res.decide_diff), 5)

    def test_append_ignore_writes_the_header_once(self):
        path = os.path.join(self.home, ".config", "proton-backup", "ignore-dropbox.txt")
        self.dc.append_ignore(path, ["Old/a.txt"])
        self.dc.append_ignore(path, ["Old/b.txt", "Other"])
        text = read(path)
        self.assertEqual(text.count("Paths deleted from Proton on purpose"), 1)
        self.assertEqual(text.count("# added by the review on"), 2)
        self.assertEqual(self.dc.load_ignore(path), ["old/a.txt", "old/b.txt", "other"])

    def test_paths_with_wildcard_characters_are_ignored_literally(self):
        path = os.path.join(self.tmp, "ignore.txt")
        self.dc.append_ignore(path, ["Scans [2019]/a.pdf"])
        patterns = self.dc.load_ignore(path)
        self.assertTrue(self.dc.is_ignored("Scans [2019]/a.pdf", patterns))


# ---------------------------------------------------------------------------
# 7. End to end, against a fake proton-drive
# ---------------------------------------------------------------------------

class EndToEndTest(DriveCompareTestCase):

    def setUp(self):
        super().setUp()
        self.cli = self.write(os.path.join(self.tmp, "bin", "proton-drive"), FAKE_CLI)
        os.chmod(self.cli, 0o755)
        self.state = os.path.join(self.tmp, "state.json")
        self.src = self.mkdir("Src")
        self.pf = self.mkdir("ProtonFolder")
        self.logs = self.plib.LOG_DIR
        env = mock.patch.dict(os.environ, {"FAKE_STATE": self.state})
        env.start()
        self.addCleanup(env.stop)

    def set_proton(self, drive, trash=(), trash_fails=False):
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"drive": {"/my-files/" + k: v for k, v in drive.items()},
                       "trash": list(trash), "trash_fails": trash_fails}, f)
        # the synced Proton Drive folder holds the same top-level folders:
        # the copy script refuses a folder that doesn't match the scan
        for rel in drive:
            if "/" in rel:
                os.makedirs(os.path.join(self.pf, rel.split("/", 1)[0]), exist_ok=True)

    def set_folder(self, files):
        for rel, text in files.items():
            mtime = None
            if isinstance(text, tuple):
                text, mtime = text
            self.write(os.path.join(self.src, rel), text, mtime)

    def run_main(self, *argv, tty=False, answers=None):
        """Run main(); returns (exit code, terminal output)."""
        argv = list(argv) + ["--cli", self.cli, "--config", self.config]
        it = iter(answers or [])

        def fake_input(prompt=""):
            try:
                return next(it)
            except StopIteration:
                self.fail(f"unexpected question: {prompt!r}")

        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["drive-compare.py"] + argv), \
                mock.patch.object(sys, "stdin", FakeStdin(tty)), \
                mock.patch("builtins.input", fake_input), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rv = self.dc.main()
        self.assertEqual(list(it), [], "not every answer was used")
        return rv, out.getvalue()

    def outputs(self):
        return sorted(os.listdir(self.logs))

    def only(self, prefix):
        found = [f for f in self.outputs() if f.startswith(prefix)]
        self.assertEqual(len(found), 1, self.outputs())
        return os.path.join(self.logs, found[0])

    def profile(self, **settings):
        lines = [f"folder = {self.src}", f"proton_folder = {self.pf}"]
        lines += [f"{k} = {v}" for k, v in settings.items()]
        self.write_config("[disk]\n" + "\n".join(lines) + "\n")

    def test_identical_folder_exits_0_with_a_single_report(self):
        self.set_proton({"a.txt": "alpha", "Docs/b.txt": "beta"})
        self.set_folder({"a.txt": "alpha", "Docs/b.txt": "beta"})
        rv, out = self.run_main(self.src)
        self.assertEqual(rv, 0)
        self.assertEqual(len(self.outputs()), 2, self.outputs())
        self.assertIn("my-files.tsv", self.outputs())
        report = read(self.only("drive-compare-folder-"))
        self.assertIn("NEEDS A DECISION: 0", report)
        self.assertIn("Nothing in folder is missing from Proton.", out)

    def test_missing_file_exits_1_and_no_copy_script_by_default(self):
        self.set_proton({"a.txt": "alpha"})
        self.set_folder({"a.txt": "alpha", "new.txt": "fresh"})
        rv, _ = self.run_main(self.src)
        self.assertEqual(rv, 1)
        self.assertEqual([f for f in self.outputs() if f.startswith("copy-to-proton")], [])
        report = read(self.only("drive-compare-folder-"))
        self.assertIn("NEEDS A DECISION: 1", report)
        self.assertIn("new.txt", report)

    def test_profile_name_is_used_in_the_report_and_file_names(self):
        self.profile()
        self.set_proton({"a.txt": "alpha"})
        self.set_folder({"a.txt": "alpha", "new.txt": "fresh"})
        self.run_main("disk")
        report = read(self.only("drive-compare-disk-"))
        self.assertIn("drive-compare: disk vs Proton", report)
        self.assertIn("Only in disk, not found anywhere on Proton", report)

    def test_moved_files_are_hidden_or_listed(self):
        self.set_proton({"New/a.txt": "alpha"})
        self.set_folder({"Old/a.txt": "alpha"})
        rv, out = self.run_main(self.src)
        self.assertEqual(rv, 0)
        self.assertNotIn("MOVED OR RENAMED ON PROTON", out)
        self.assertRegex(out, r"moved or renamed on Proton \.+ +1")
        rv, out = self.run_main(self.src, "--moved", "list", "--reuse-scan")
        self.assertIn("Old/a.txt  ->  New/a.txt", out)

    def test_proton_only_files_are_hidden_or_listed(self):
        self.set_proton({"a.txt": "alpha", "Extra/z.txt": "zeta"})
        self.set_folder({"a.txt": "alpha"})
        rv, out = self.run_main(self.src)
        self.assertEqual(rv, 0)
        self.assertNotIn("ONLY ON PROTON:", out)
        _, out = self.run_main(self.src, "--proton-only", "list", "--reuse-scan")
        self.assertIn("ONLY ON PROTON: 1", out)
        self.assertIn("Extra/z.txt", out)

    def test_excluded_folders_are_skipped_on_both_sides(self):
        self.set_proton({"a.txt": "alpha", "Photos/p.jpg": "p", "Camera/c.jpg": "c"})
        self.set_folder({"a.txt": "alpha", "Photos/q.jpg": "q"})
        rv, out = self.run_main(self.src, "--exclude", "Photos,Camera")
        self.assertEqual(rv, 0)
        self.assertNotIn("only on Proton", out)

    def test_details_appends_full_lists_to_the_report_only(self):
        self.set_proton({"a.txt": "alpha", "Extra/z.txt": "zeta"})
        self.set_folder({"a.txt": "alpha"})
        _, out = self.run_main(self.src, "--details")
        report = read(self.only("drive-compare-folder-"))
        self.assertIn("DETAILS", report)
        self.assertIn("Only on Proton: 1\n    Extra/z.txt", report)
        self.assertNotIn("DETAILS", out)

    def test_trash_hints_reach_the_report(self):
        self.set_proton({"a.txt": "alpha"},
                        trash=[{"type": "file", "name": "bye.txt", "content": "bye",
                                "when": "2026-10-12T10:00:00Z"},
                               {"type": "folder", "name": "Gone", "when": "2026-10-20T10:00:00Z"}])
        self.set_folder({"a.txt": "alpha", "bye.txt": "bye", "Gone/x.txt": "x"})
        _, out = self.run_main(self.src)
        self.assertIn("bye.txt   3.0 B", out)
        self.assertIn("[in the Proton trash since 2026-10-12]", out)
        self.assertIn("[folder 'Gone' is in the Proton trash since 2026-10-20]", out)

    def test_unreadable_trash_is_reported_and_the_run_continues(self):
        self.set_proton({"a.txt": "alpha"}, trash_fails=True)
        self.set_folder({"a.txt": "alpha", "new.txt": "n"})
        rv, out = self.run_main(self.src)
        self.assertEqual(rv, 1)
        self.assertIn("the Proton trash couldn't be read", out)

    def test_ignore_file_hides_files_deleted_on_purpose(self):
        self.profile()
        self.write(os.path.join(self.home, ".config", "proton-backup", "ignore-disk.txt"),
                   "Old Project\n")
        self.set_proton({"a.txt": "alpha"})
        self.set_folder({"a.txt": "alpha", "Old Project/x.txt": "x", "Old Project/y.txt": "y"})
        rv, out = self.run_main("disk")
        self.assertEqual(rv, 0)
        self.assertRegex(out, r"deleted on purpose \(ignore file\) \.+ +2")

    # interactive review

    def two_missing(self):
        self.set_proton({"a.txt": "alpha", "Docs/keep.txt": "k"})
        self.set_folder({"a.txt": "alpha", "new.txt": "n", "gone.txt": "g"})

    def test_review_runs_in_a_terminal_and_saves_choices(self):
        self.profile()
        self.two_missing()
        rv, out = self.run_main("disk", tty=True, answers=["i", "c"])
        self.assertIn("2 files need a decision", out)
        ignore = os.path.join(self.home, ".config", "proton-backup", "ignore-disk.txt")
        self.assertEqual(self.dc.load_ignore(ignore), ["gone.txt"])
        script = read(self.only("copy-to-proton-disk-"))
        self.assertIn("copy_file " + os.path.join(self.src, "new.txt"), script)
        self.assertNotIn("gone.txt", script)
        self.assertEqual(rv, 1)    # something to copy
        # the next run sees the choice: gone.txt is now ignored
        rv, out = self.run_main("disk", "--reuse-scan", tty=True, answers=["s"])
        self.assertRegex(out, r"deleted on purpose \(ignore file\) \.+ +1")

    def test_no_review_without_a_terminal(self):
        self.two_missing()
        rv, out = self.run_main(self.src, tty=False)
        self.assertNotIn("need a decision. For each one", out)
        self.assertEqual(rv, 1)

    def test_no_review_with_the_no_review_option(self):
        self.two_missing()
        _, out = self.run_main(self.src, "--no-review", tty=True)
        self.assertNotIn("need a decision. For each one", out)

    def test_no_review_when_review_max_is_zero(self):
        self.two_missing()
        _, out = self.run_main(self.src, "--review-max", "0", tty=True)
        self.assertNotIn("need a decision. For each one", out)

    def test_no_review_above_review_max(self):
        self.two_missing()
        _, out = self.run_main(self.src, "--review-max", "1", tty=True)
        self.assertNotIn("need a decision. For each one", out)
        self.assertIn("NEEDS A DECISION: 2", out)

    def test_review_at_exactly_review_max(self):
        self.two_missing()
        _, out = self.run_main(self.src, "--review-max", "2", tty=True, answers=["s", "s"])
        self.assertIn("2 files need a decision", out)

    def test_no_review_when_nothing_to_decide(self):
        self.set_proton({"a.txt": "alpha"})
        self.set_folder({"a.txt": "alpha"})
        rv, _ = self.run_main(self.src, tty=True)
        self.assertEqual(rv, 0)

    # copy script

    def test_copy_script_yes_puts_missing_files_in_the_script(self):
        self.profile(copy_script="yes")
        self.two_missing()
        rv, out = self.run_main("disk")
        self.assertEqual(rv, 1)
        script = read(self.only("copy-to-proton-disk-"))
        self.assertEqual(script.count("\ncopy_file "), 2)
        self.assertIn("(in the copy script)", out)

    def test_winner_folder_writes_replace_lines(self):
        self.profile(winner="folder")
        self.set_proton({"Docs/cv.txt": "old cv", "a.txt": "alpha"})
        self.set_folder({"Docs/cv.txt": "new cv!", "a.txt": "alpha"})
        rv, _ = self.run_main("disk")
        self.assertEqual(rv, 1)
        script = read(self.only("copy-to-proton-disk-"))
        self.assertIn("replace_file " + os.path.join(self.src, "Docs", "cv.txt"), script)
        self.assertNotIn("\ncopy_file ", script)

    def test_winner_proton_writes_no_script(self):
        self.profile(winner="proton")
        self.set_proton({"Docs/cv.txt": "old cv"})
        self.set_folder({"Docs/cv.txt": "new cv!"})
        rv, out = self.run_main("disk")
        self.assertEqual(rv, 0)
        self.assertEqual([f for f in self.outputs() if f.startswith("copy-to-proton")], [])
        self.assertRegex(out, r"different version, Proton's kept \.+ +1")

    def test_copy_script_copies_without_overwriting_and_replaces_only_its_targets(self):
        self.profile(copy_script="yes", winner="folder")
        self.set_proton({"same.txt": "same", "Docs/cv.txt": "old cv", "Docs/keep.txt": "k"})
        self.set_folder({"same.txt": "same", "Docs/cv.txt": "new cv!", "Docs/keep.txt": "k",
                         "new.txt": "brand new", "already.txt": "folder version",
                         "Sub dir/it's \"odd\".txt": "odd"})
        # the Proton Drive folder: the synced top-level folder exists, one
        # missing file is already there (not synced yet), one version to replace
        self.write(os.path.join(self.pf, "Docs", "cv.txt"), "old cv")
        self.write(os.path.join(self.pf, "already.txt"), "proton folder version")
        self.run_main("disk")
        script = self.only("copy-to-proton-disk-")
        self.assertEqual(subprocess.run(["bash", "-n", script]).returncode, 0)
        bash = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")
        r = subprocess.run([bash, script], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Copied: 2   Replaced: 1   Skipped: 0   Already there: 1   Failed: 0",
                      r.stdout)

        def in_pf(rel):
            return read(os.path.join(self.pf, rel))
        self.assertEqual(in_pf("new.txt"), "brand new")
        self.assertEqual(in_pf("Sub dir/it's \"odd\".txt"), "odd")
        self.assertEqual(in_pf("already.txt"), "proton folder version")   # not overwritten
        self.assertEqual(in_pf("Docs/cv.txt"), "new cv!")                 # replaced
        self.assertFalse(os.path.exists(os.path.join(self.pf, "same.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.pf, "Docs", "keep.txt")))

    def test_copy_script_skips_files_empty_at_run_time(self):
        self.profile(copy_script="yes")
        self.set_proton({"Docs/a.txt": "alpha"})
        self.set_folder({"Docs/a.txt": "alpha", "new.txt": "will be emptied"})
        self.run_main("disk")
        open(os.path.join(self.src, "new.txt"), "w").close()
        r = subprocess.run(["bash", self.only("copy-to-proton-disk-")], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        self.assertIn("Skipped: 1", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.pf, "new.txt")))

    def test_copy_script_refuses_a_proton_folder_that_does_not_match(self):
        other = self.mkdir("Unrelated")
        self.write_config(f"[disk]\nfolder = {self.src}\nproton_folder = {other}\n"
                          "copy_script = yes\n")
        self.set_proton({"Docs/a.txt": "alpha"})
        self.set_folder({"Docs/a.txt": "alpha", "new.txt": "n"})
        rv, out = self.run_main("disk")
        self.assertIn("none of the scanned top-level items", out)
        self.assertEqual([f for f in self.outputs() if f.startswith("copy-to-proton")], [])
        self.assertEqual(rv, 1)


if __name__ == "__main__":
    unittest.main()
