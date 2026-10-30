# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
Unit tests for protonlib.py: the configuration file shared by the scripts
(profiles, [DEFAULT], validation, writing it back, --setup mechanics) and a
few small helpers.

Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import REPO, FakeStdin, ScriptTestCase, read, sha1  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Choosing the profile
# ---------------------------------------------------------------------------

class ChooseProfileTest(ScriptTestCase):

    def choose(self, target, script="drive-compare"):
        cfg = self.plib.read_config(self.config)
        return self.plib.choose_profile(cfg, script, target, self.config)

    def test_section_names_are_script_then_profile(self):
        self.assertEqual(self.plib.profile_section("file-backup", "ssd"), "file-backup ssd")

    def test_profile_names_lists_only_that_scripts_sections(self):
        self.write_config("""
            [drive-compare dropbox]
            [drive-compare disk]
            [file-backup ssd]
            [plain]
            """)
        cfg = self.plib.read_config(self.config)
        self.assertEqual(self.plib.profile_names(cfg, "drive-compare"), ["dropbox", "disk"])
        self.assertEqual(self.plib.profile_names(cfg, "file-backup"), ["ssd"])

    def test_no_target_uses_the_only_profile_of_the_script(self):
        self.write_config("""
            [drive-compare dropbox]
            [file-backup ssd]
            """)
        self.assertEqual(self.choose(None), "dropbox")
        self.assertEqual(self.choose(None, "file-backup"), "ssd")

    def test_no_target_and_no_profile_suggests_setup(self):
        self.assertExits(lambda: self.choose(None),
                         "no drive-compare profile", self.config,
                         "./drive-compare.py --setup NAME")

    def test_no_target_ignores_the_profiles_of_other_scripts(self):
        self.write_config("""
            [file-backup ssd]
            """)
        self.assertExits(lambda: self.choose(None), "no drive-compare profile")

    def test_no_target_with_several_profiles_asks_to_name_one(self):
        self.write_config("""
            [drive-compare dropbox]
            [drive-compare disk]
            """)
        self.assertExits(lambda: self.choose(None),
                         "several drive-compare profiles", "dropbox, disk", "Name the one")

    def test_profile_name_is_returned(self):
        self.write_config("""
            [drive-compare dropbox]
            [drive-compare disk]
            """)
        self.assertEqual(self.choose("disk"), "disk")

    def test_path_like_targets_are_never_profiles(self):
        self.write_config("""
            [drive-compare docs]
            [drive-compare ~docs]
            """)
        for target in ("./docs", "~/docs", "/docs", "a/docs", "~docs", ".docs"):
            with self.subTest(target=target):
                self.assertIsNone(self.choose(target))

    def test_existing_folder_that_is_not_a_profile_is_a_path(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.mkdir("cwd"))
        os.makedirs("Backup")
        self.assertIsNone(self.choose("Backup"))

    def test_profile_wins_over_a_folder_with_the_same_name(self):
        self.write_config("""
            [drive-compare docs]
            """)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.mkdir("cwd"))
        os.makedirs("docs")
        self.assertEqual(self.choose("docs"), "docs")
        self.assertIsNone(self.choose("./docs"))

    def test_old_format_section_says_how_to_rename_it(self):
        self.write_config("""
            [dropbox]
            folder = /x
            """)
        self.assertExits(lambda: self.choose("dropbox"),
                         "[dropbox]", "old format", "[drive-compare dropbox]")
        self.assertExits(lambda: self.choose("dropbox", "file-backup"),
                         "[file-backup dropbox]")

    def test_unknown_profile_lists_this_scripts_profiles_and_suggests_setup(self):
        self.write_config("""
            [drive-compare dropbox]
            [drive-compare disk]
            [file-backup ssd]
            """)
        msg = self.assertExits(lambda: self.choose("nope"),
                               "'nope' is neither a drive-compare profile",
                               "(profiles: dropbox, disk)",
                               "./drive-compare.py --setup nope")
        self.assertNotIn("ssd", msg)

    def test_unknown_profile_without_config_says_none_yet(self):
        self.assertExits(lambda: self.choose("nope", "file-backup"),
                         "profiles: none yet", "./file-backup.py --setup nope")

    def test_profile_of_another_script_is_not_found(self):
        self.write_config("""
            [file-backup ssd]
            """)
        self.assertExits(lambda: self.choose("ssd"), "'ssd' is neither a drive-compare profile")


# ---------------------------------------------------------------------------
# 2. Loading and validating the settings
# ---------------------------------------------------------------------------

class LoadSettingsTest(ScriptTestCase):

    def load(self, script, name):
        cfg = self.plib.read_config(self.config)
        return self.plib.load_settings(cfg, script, name)

    def test_every_script_has_defaults_and_explanations(self):
        for script, settings in self.plib.PROFILE_SETTINGS.items():
            for key, default, help_text in settings:
                with self.subTest(script=script, key=key):
                    self.assertTrue(help_text)
                    self.assertEqual(self.plib.setting_help(key) is not None, True)
                    self.assertEqual(self.plib.profile_defaults(script)[key], default)

    def test_setting_help_of_an_unknown_key_is_none(self):
        self.assertIsNone(self.plib.setting_help("colour"))

    def test_defaults_then_default_section_then_profile(self):
        self.write_config("""
            [DEFAULT]
            winner = newer
            moved = list

            [drive-compare dropbox]
            folder = ~/Dropbox
            moved = hide
            """)
        s = self.load("drive-compare", "dropbox")
        self.assertEqual(s["folder"], "~/Dropbox")
        self.assertEqual(s["winner"], "newer")      # from [DEFAULT]
        self.assertEqual(s["moved"], "hide")        # the profile wins
        self.assertEqual(s["compare_by"], "content")  # built-in default

    def test_without_a_profile_defaults_and_default_section(self):
        self.write_config("""
            [DEFAULT]
            verify = no

            [file-backup ssd]
            dest = /Volumes/Disk
            batch = 5
            """)
        s = self.load("file-backup", None)
        self.assertEqual(s["verify"], "no")
        self.assertEqual(s["batch"], "20")
        self.assertEqual(s["dest"], "")

    def test_a_profile_does_not_leak_into_another_script(self):
        self.write_config("""
            [drive-compare same]
            winner = ask
            review_max = 3

            [file-backup same]
            dest = /Volumes/Disk
            """)
        fb = self.load("file-backup", "same")
        dc = self.load("drive-compare", "same")
        self.assertNotIn("winner", fb)
        self.assertNotIn("review_max", fb)
        self.assertNotIn("dest", dc)
        self.assertEqual(dc["winner"], "ask")

    def test_default_section_is_shared_by_every_script(self):
        self.write_config("""
            [DEFAULT]
            proton_root = /my-files/Work

            [drive-compare dropbox]
            [file-backup ssd]
            """)
        self.assertEqual(self.load("drive-compare", "dropbox")["proton_root"], "/my-files/Work")
        self.assertEqual(self.load("file-backup", "ssd")["proton_root"], "/my-files/Work")

    def test_inline_comments_are_stripped(self):
        self.write_config("""
            [drive-compare p]
            folder = /data   ; the cloud folder
            winner = ask     # decide by hand
            """)
        s = self.load("drive-compare", "p")
        self.assertEqual(s["folder"], "/data")
        self.assertEqual(s["winner"], "ask")

    def test_percent_is_taken_literally(self):
        self.write_config("""
            [file-backup p]
            dest = /Volumes/100%done
            """)
        self.assertEqual(self.load("file-backup", "p")["dest"], "/Volumes/100%done")

    def test_missing_config_reads_as_empty(self):
        cfg = self.plib.read_config(os.path.join(self.tmp, "none.ini"))
        self.assertEqual(cfg.sections(), [])

    def test_unreadable_config_is_reported(self):
        self.write_config("this is not ini\n[ok]\n")
        self.assertExits(lambda: self.plib.read_config(self.config), "can't read", self.config)


class CheckSettingsTest(ScriptTestCase):

    def test_valid_values_pass_and_numbers_become_ints(self):
        s = {"winner": "ask", "verify": "no", "review_max": "0", "workers": "4",
             "batch": "1", "timeout": "60", "folder": "/x"}
        self.plib.check_settings(s, "here")
        self.assertEqual((s["review_max"], s["workers"], s["batch"], s["timeout"]), (0, 4, 1, 60))
        self.assertEqual(s["folder"], "/x")

    def test_settings_not_present_are_not_checked(self):
        s = {"folder": "/x"}
        self.plib.check_settings(s, "here")
        self.assertEqual(s, {"folder": "/x"})

    def test_every_choice_is_validated_with_its_allowed_values(self):
        for key, allowed in self.plib.SETTING_CHOICES.items():
            with self.subTest(key=key):
                self.assertExits(lambda: self.plib.check_settings({key: "bogus"}, "profile [x]"),
                                 "profile [x]", f"{key} = 'bogus'", ", ".join(allowed))
                for ok in allowed:
                    self.plib.check_settings({key: ok}, "here")

    def test_every_number_has_its_minimum(self):
        for key, low in self.plib.SETTING_MINIMUM.items():
            with self.subTest(key=key):
                for bad in ("abc", str(low - 1), "2.5", ""):
                    self.assertExits(lambda: self.plib.check_settings({key: bad}, "where"),
                                     "where", f"{key} must be a number, {low} or more")
                s = {key: str(low)}
                self.plib.check_settings(s, "where")
                self.assertEqual(s[key], low)

    def test_expected_minimums(self):
        self.assertEqual(self.plib.SETTING_MINIMUM,
                         {"review_max": 0, "workers": 1, "batch": 1, "timeout": 1,
                          "tolerance": 0, "wait": 0})


# ---------------------------------------------------------------------------
# 3. Writing the configuration
# ---------------------------------------------------------------------------

class ConfigFileTest(ScriptTestCase):

    def make_cfg(self):
        cfg = self.plib.read_config(os.path.join(self.tmp, "none.ini"))
        cfg["DEFAULT"]["compare_by"] = "size"
        cfg.add_section("drive-compare dropbox")
        cfg.set("drive-compare dropbox", "folder", "~/Dropbox")
        cfg.set("drive-compare dropbox", "winner", "ask")
        cfg.set("drive-compare dropbox", "colour", "blue")    # unknown key, by hand
        cfg.add_section("file-backup ssd")
        cfg.set("file-backup ssd", "dest", "/Volumes/Disk/100%")
        cfg.set("file-backup ssd", "verify", "yes")
        cfg.add_section("other-tool x")
        cfg.set("other-tool x", "zeta", "1")
        cfg.set("other-tool x", "alpha", "2")
        return cfg

    def section(self, text, name):
        # the header also mentions section names: match whole lines only
        return text.split(f"\n[{name}]\n")[1].split("\n[")[0]

    def test_round_trip_keeps_every_value(self):
        self.plib.write_config(self.config, self.make_cfg())
        back = self.plib.read_config(self.config)
        self.assertEqual(back.sections(), ["drive-compare dropbox", "file-backup ssd", "other-tool x"])
        self.assertEqual(back.defaults()["compare_by"], "size")
        self.assertEqual(back.get("drive-compare dropbox", "winner"), "ask")
        self.assertEqual(back.get("drive-compare dropbox", "colour"), "blue")
        self.assertEqual(back.get("file-backup ssd", "dest"), "/Volumes/Disk/100%")
        self.assertEqual(back.get("file-backup ssd", "compare_by"), "size")    # inherited

    def test_header_explains_the_section_names(self):
        text = self.plib.render_config(self.make_cfg())
        self.assertTrue(text.startswith("#"))
        self.assertIn("[<script> <profile>]", text)

    def test_every_known_setting_is_preceded_by_its_explanation(self):
        lines = self.plib.render_config(self.make_cfg()).splitlines()
        for key in ("folder", "winner", "compare_by", "dest", "verify"):
            with self.subTest(key=key):
                i = next(n for n, l in enumerate(lines) if l.startswith(f"{key} = "))
                self.assertEqual(lines[i - 1], f"; {self.plib.setting_help(key)}")

    # proton_root is declared by both scripts, with different explanations
    def test_shared_key_gets_the_explanation_of_its_own_script(self):
        cfg = self.plib.read_config(os.path.join(self.tmp, "none.ini"))
        cfg.add_section("file-backup ssd")
        cfg.set("file-backup ssd", "proton_root", "/my-files")
        lines = self.plib.render_config(cfg).splitlines()
        i = lines.index("proton_root = /my-files")
        fb_help = dict((k, h) for k, _, h in self.plib.PROFILE_SETTINGS["file-backup"])
        self.assertEqual(lines[i - 1], f"; {fb_help['proton_root']}")

    def test_default_section_is_written_first(self):
        text = self.plib.render_config(self.make_cfg())
        self.assertLess(text.index("\n[DEFAULT]\n"), text.index("\n[drive-compare dropbox]\n"))

    def test_no_default_section_when_there_are_no_defaults(self):
        cfg = self.plib.read_config(os.path.join(self.tmp, "none.ini"))
        cfg.add_section("file-backup ssd")
        self.assertNotIn("\n[DEFAULT]\n", self.plib.render_config(cfg))

    def test_profiles_keep_only_their_own_keys(self):
        text = self.plib.render_config(self.make_cfg())
        self.assertNotIn("compare_by", self.section(text, "drive-compare dropbox"))
        self.assertNotIn("compare_by", self.section(text, "file-backup ssd"))

    def test_known_keys_follow_the_scripts_order(self):
        cfg = self.plib.read_config(os.path.join(self.tmp, "none.ini"))
        cfg.add_section("file-backup ssd")
        for k in ("timeout", "verify", "dest"):
            cfg.set("file-backup ssd", k, "1")
        sec = self.section(self.plib.render_config(cfg), "file-backup ssd")
        self.assertLess(sec.index("dest ="), sec.index("verify ="))
        self.assertLess(sec.index("verify ="), sec.index("timeout ="))

    def test_unknown_keys_are_kept_after_the_known_ones(self):
        sec = self.section(self.plib.render_config(self.make_cfg()), "drive-compare dropbox")
        self.assertIn("colour = blue", sec)
        self.assertLess(sec.index("winner = "), sec.index("colour = "))

    def test_unknown_script_sections_are_kept_without_comments(self):
        sec = self.section(self.plib.render_config(self.make_cfg()), "other-tool x")
        self.assertNotIn(";", sec)
        self.assertLess(sec.index("alpha = 2"), sec.index("zeta = 1"))

    def test_write_creates_the_config_folder(self):
        self.assertFalse(os.path.exists(os.path.dirname(self.config)))
        self.plib.write_config(self.config, self.make_cfg())
        self.assertTrue(os.path.isfile(self.config))
        self.assertEqual(os.listdir(os.path.dirname(self.config)), ["config.ini"])

    def test_write_through_a_symlink_keeps_the_link(self):
        # e.g. config.ini linked to a file kept in a private repository
        target = self.write(os.path.join(self.tmp, "private-repo", "config.ini"), "[old]\n")
        os.makedirs(os.path.dirname(self.config))
        os.symlink(target, self.config)
        self.plib.write_config(self.config, self.make_cfg())
        self.assertTrue(os.path.islink(self.config))
        self.assertEqual(os.path.realpath(self.config), target)
        self.assertIn("file-backup ssd", self.plib.read_config(target).sections())
        self.assertEqual(os.listdir(os.path.dirname(target)), ["config.ini"])

    def test_failed_write_leaves_the_old_file_untouched(self):
        self.write_config("[old]\nfolder = /x\n")
        with mock.patch.object(self.plib.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.plib.write_config(self.config, self.make_cfg())
        self.assertEqual(read(self.config), "[old]\nfolder = /x\n")

    def test_example_config_in_the_repo_is_valid(self):
        example = os.path.join(REPO, "config.example.ini")
        cfg = self.plib.read_config(example)
        self.assertTrue(cfg.sections())
        for section in cfg.sections():
            with self.subTest(section=section):
                script, _, name = section.partition(" ")
                self.assertIn(script, self.plib.PROFILE_SETTINGS)
                self.assertRegex(name, self.plib.PROFILE_RE)
                s = self.plib.load_settings(cfg, script, name)
                known = set(self.plib.profile_defaults(script)) | set(cfg.defaults())
                base = self.plib.PROFILE_BASE.get(script)
                if base:
                    # the backup folder comes from the base script's profile
                    self.assertTrue(cfg.has_section(self.plib.profile_section(base, name)))
                    self.assertTrue(s.pop("dest"))
                self.assertEqual(set(s) - known, set(), "unknown keys")
                self.plib.check_settings(s, f"[{section}] in config.example.ini")


# ---------------------------------------------------------------------------
# 4. Questions and --setup mechanics
# ---------------------------------------------------------------------------

class AskTest(ScriptTestCase):

    def ask(self, answers, *args, **kw):
        out = io.StringIO()
        with self.answering(answers), contextlib.redirect_stdout(out):
            return self.plib.ask(*args, **kw), out.getvalue()

    def test_enter_keeps_the_default(self):
        self.assertEqual(self.ask([""], "winner", "proton")[0], "proton")

    def test_numeric_default_is_returned_as_text(self):
        self.assertEqual(self.ask([""], "batch", 20, minimum=1)[0], "20")

    def test_answer_is_stripped(self):
        self.assertEqual(self.ask(["  ask  "], "winner", "proton")[0], "ask")

    def test_prompt_shows_choices_and_default(self):
        with self.answering(["ask"]) as prompts, contextlib.redirect_stdout(io.StringIO()):
            self.plib.ask("winner", "proton", ("proton", "ask"))
        self.assertIn("winner [proton/ask] (proton)", prompts[0])

    def test_invalid_choice_is_asked_again(self):
        value, out = self.ask(["maybe", "ask"], "winner", "proton", ("proton", "ask"))
        self.assertEqual(value, "ask")
        self.assertIn("answer one of: proton, ask", out)

    def test_number_below_minimum_or_not_a_number_is_asked_again(self):
        value, out = self.ask(["ten", "-3", "0", "2"], "workers", "3", None, 1)
        self.assertEqual(value, "2")
        self.assertEqual(out.count("a number, 1 or more"), 3)

    def test_end_of_input_quits_like_ctrl_c(self):
        with self.answering([EOFError()]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                self.plib.ask("winner", "proton")

    def test_existing_folder_is_accepted(self):
        folder = self.mkdir("Disk")
        with self.answering([folder]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.plib.ask_existing_folder("dest", "/x"), folder)

    def test_missing_folder_can_be_kept_anyway(self):
        out = io.StringIO()
        with self.answering(["/Volumes/NotPluggedIn", "yes"]), contextlib.redirect_stdout(out):
            self.assertEqual(self.plib.ask_existing_folder("dest", "/x"), "/Volumes/NotPluggedIn")
        self.assertIn("doesn't exist", out.getvalue())

    def test_missing_folder_is_asked_again_when_not_kept(self):
        folder = self.mkdir("Disk")
        with self.answering(["/Volumes/NotPluggedIn", "", folder]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.plib.ask_existing_folder("dest", "/x"), folder)

    def test_tilde_folder_is_checked_expanded_but_saved_as_typed(self):
        os.makedirs(os.path.join(self.home, "Backup"))
        with self.answering(["~/Backup"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.plib.ask_existing_folder("dest", "/x"), "~/Backup")


class RunSetupTest(ScriptTestCase):
    """run_setup() with a trivial questions function: one setting, `answer`."""

    def setup(self, answers, name=None, script="drive-compare", tty=True, seen=None):
        def questions(cur):
            if seen is not None:
                seen.update(cur)
            return {"winner": self.plib.ask("winner", cur["winner"])} \
                if script == "drive-compare" else {"dest": self.plib.ask("dest", cur["dest"])}

        out = io.StringIO()
        with self.answering(answers, tty=tty), contextlib.redirect_stdout(out):
            rv = self.plib.run_setup(self.config, script, name, questions)
        return rv, out.getvalue()

    def saved(self):
        return self.plib.read_config(self.config)

    def test_new_profile_is_written_in_its_section(self):
        rv, out = self.setup(["ask"], "dropbox")
        self.assertEqual(rv, 0)
        self.assertEqual(dict(self.saved().items("drive-compare dropbox")), {"winner": "ask"})
        self.assertIn("New profile [drive-compare dropbox]", out)
        self.assertIn("Saved profile [drive-compare dropbox]", out)

    def test_a_single_profile_can_be_run_without_its_name(self):
        _, out = self.setup(["/Volumes/Disk"], "ssd", script="file-backup")
        self.assertIn("./file-backup.py   (or ./file-backup.py ssd)", out)

    def test_with_several_profiles_the_name_is_needed(self):
        self.write_config("[file-backup old]\ndest = /x\n")
        _, out = self.setup(["/Volumes/Disk"], "ssd", script="file-backup")
        self.assertIn("Run it with:  ./file-backup.py ssd", out)
        self.assertIn("existing file-backup profiles: old", out)

    def test_profile_name_is_asked_when_missing(self):
        self.setup(["ssd", "ask"])
        self.assertEqual(self.saved().sections(), ["drive-compare ssd"])

    def test_invalid_profile_name_is_asked_again(self):
        _, out = self.setup(["also bad!", "", "ssd", "ask"], "bad name")
        self.assertEqual(self.saved().sections(), ["drive-compare ssd"])
        self.assertEqual(out.count("letters, digits"), 2)

    def test_changing_a_profile_keeps_the_others_of_every_script(self):
        self.write_config("""
            [DEFAULT]
            proton_root = /my-files/Work

            [drive-compare dropbox]
            folder = /data
            winner = ask

            [drive-compare disk]
            folder = /Volumes/Disk
            moved = list

            [file-backup ssd]
            dest = /Volumes/Disk/backup
            """)
        _, out = self.setup(["newer"], "dropbox")
        cfg = self.saved()
        self.assertIn("Changing profile [drive-compare dropbox]", out)
        self.assertEqual(cfg.get("drive-compare dropbox", "winner"), "newer")
        self.assertEqual(cfg.get("drive-compare dropbox", "folder"), "/data")
        self.assertEqual(cfg.get("drive-compare disk", "moved"), "list")
        self.assertEqual(cfg.get("file-backup ssd", "dest"), "/Volumes/Disk/backup")
        self.assertEqual(cfg.defaults()["proton_root"], "/my-files/Work")

    def test_current_values_come_from_defaults_default_section_and_profile(self):
        self.write_config("""
            [DEFAULT]
            winner = newer

            [drive-compare dropbox]
            folder = /data
            """)
        seen = {}
        self.setup([""], "dropbox", seen=seen)
        self.assertEqual(seen["winner"], "newer")
        self.assertEqual(seen["folder"], "/data")
        self.assertEqual(seen["compare_by"], "content")
        self.assertEqual(self.saved().get("drive-compare dropbox", "winner"), "newer")

    def test_same_name_in_another_script_is_a_new_profile(self):
        self.write_config("[drive-compare ssd]\nwinner = ask\n")
        _, out = self.setup(["/Volumes/Disk"], "ssd", script="file-backup")
        self.assertIn("New profile [file-backup ssd]", out)
        self.assertEqual(self.saved().get("drive-compare ssd", "winner"), "ask")

    def test_setup_through_a_symlink_keeps_the_link(self):
        target = self.write(os.path.join(self.tmp, "private", "config.ini"), "")
        os.makedirs(os.path.dirname(self.config))
        os.symlink(target, self.config)
        self.setup(["ask"], "dropbox")
        self.assertTrue(os.path.islink(self.config))
        self.assertIn("[drive-compare dropbox]", read(target))

    def test_refuses_without_a_terminal(self):
        self.assertExits(lambda: self.setup([], "dropbox", tty=False), "run it in a terminal")
        self.assertFalse(os.path.exists(self.config))

    def test_ctrl_c_in_a_question_writes_nothing(self):
        self.assertExits(lambda: self.setup([KeyboardInterrupt()], "dropbox"), "nothing written")
        self.assertFalse(os.path.exists(self.config))

    def test_end_of_input_in_a_question_writes_nothing(self):
        self.assertExits(lambda: self.setup([EOFError()], "dropbox"), "nothing written")
        self.assertFalse(os.path.exists(self.config))

    def test_end_of_input_at_the_profile_name_writes_nothing(self):
        self.assertExits(lambda: self.setup([EOFError()]), "nothing written")
        self.assertFalse(os.path.exists(self.config))

    def test_ctrl_c_on_an_existing_config_leaves_it_untouched(self):
        self.write_config("[drive-compare keep]\nwinner = ask\n")
        before = read(self.config)
        self.assertExits(lambda: self.setup([KeyboardInterrupt()], "keep"), "nothing written")
        self.assertEqual(read(self.config), before)


# ---------------------------------------------------------------------------
# 5. Small helpers
# ---------------------------------------------------------------------------

class ScanFilesTest(ScriptTestCase):

    def test_scan_path_is_named_after_the_root(self):
        logs = self.plib.LOG_DIR
        self.assertEqual(self.plib.scan_path("/my-files"), os.path.join(logs, "my-files.tsv"))
        self.assertEqual(self.plib.scan_path("/my-files/Work/"),
                         os.path.join(logs, "my-files-Work.tsv"))
        self.assertEqual(self.plib.scan_path("/"), os.path.join(logs, "root.tsv"))

    def test_read_scan_parses_rows_and_skips_broken_ones(self):
        tsv = self.write(os.path.join(self.tmp, "scan.tsv"),
                         "a.txt\t5\t100\tabc\t-\n"
                         "short\t1\n"
                         "\t1\t2\t3\t-\n"
                         "b.txt\tx\ty\t-\tENCRYPTED_SIZE\n")
        self.assertEqual(self.plib.read_scan(tsv),
                         [("a.txt", 5, 100, "abc", "-"),
                          ("b.txt", 0, 0, "-", "ENCRYPTED_SIZE")])

    def test_as_local_replaces_characters_macos_cannot_store(self):
        self.assertEqual(self.plib.as_local('a:b|c*d?e<f>g"h.txt'), "a_b_c_d_e_f_g_h.txt")



# ---------------------------------------------------------------------------
# 8. The Drive scan: last complete scan kept, resume, reuse hint
# ---------------------------------------------------------------------------

class ScanDriveTest(ScriptTestCase):

    def setUp(self):
        super().setUp()
        self.cli = self.fake_cli()
        self.tsv = self.plib.scan_path("/my-files")
        os.makedirs(os.path.dirname(self.tsv), exist_ok=True)
        # no real waiting between retries or calls
        sleep = mock.patch.object(self.plib.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def drive(self, files, fail_list=()):
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"drive": files, "fail_list": list(fail_list)}, f)

    def scan(self, tty=False):
        out = io.StringIO()
        with mock.patch.object(self.plib.sys, "stdin", FakeStdin(tty)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            tsv, errors = self.plib.scan_drive(self.cli, "/my-files", workers=2,
                                               delay=0, retries=1)
        return tsv, errors, out.getvalue()

    def paths(self, path):
        return sorted(r[0] for r in self.plib.read_scan(path))

    def age(self, minutes):
        t = time.time() - minutes * 60
        os.utime(self.tsv, (t, t))

    def test_complete_scan_replaces_the_tsv_and_cleans_up(self):
        self.drive({"/my-files/a.txt": "a", "/my-files/Sub/b.txt": "b"})
        tsv, errors, _ = self.scan()
        self.assertEqual((tsv, errors), (self.tsv, []))
        self.assertEqual(self.paths(self.tsv), ["Sub/b.txt", "a.txt"])
        for ext in (".partial", ".state", ".queue"):
            self.assertFalse(os.path.exists(self.tsv + ext), ext)
        self.assertTrue(self.plib.scan_complete(self.tsv))

    def test_incomplete_scan_keeps_the_last_complete_one(self):
        self.drive({"/my-files/a.txt": "a"})
        self.scan()
        self.drive({"/my-files/a.txt": "a", "/my-files/New/n.txt": "n"},
                   fail_list=["/my-files/New"])
        _, errors, _ = self.scan()
        self.assertTrue(errors)
        self.assertEqual(self.paths(self.tsv), ["a.txt"])
        self.assertTrue(os.path.exists(self.tsv + ".partial"))
        self.assertTrue(self.plib.scan_complete(self.tsv))

    def test_interrupted_scan_resumes_and_then_replaces_the_tsv(self):
        self.drive({"/my-files/a.txt": "a"})
        self.scan()
        self.drive({"/my-files/a.txt": "a", "/my-files/New/n.txt": "n"},
                   fail_list=["/my-files/New"])
        self.scan()
        self.drive({"/my-files/a.txt": "a", "/my-files/New/n.txt": "n"})
        _, errors, out = self.scan()
        self.assertEqual(errors, [])
        self.assertIn("Resuming", out)
        self.assertEqual(self.paths(self.tsv), ["New/n.txt", "a.txt"])
        # only the folder that failed was listed again
        lists = [c[2] for c in self.cli_calls("filesystem", "list")]
        self.assertEqual(lists.count("/my-files"), 2)
        self.assertFalse(os.path.exists(self.tsv + ".partial"))

    def test_scan_interrupted_before_partial_files_existed_resumes(self):
        # the old scanner wrote straight into the TSV, with .state next to it
        self.drive({"/my-files/a.txt": "a", "/my-files/Sub/b.txt": "b"})
        with open(self.tsv + ".state", "w") as f:
            f.write("/my-files\n")
        with open(self.tsv + ".queue", "w") as f:
            f.write("/my-files/Sub\tSub\n")
        with open(self.tsv, "w") as f:
            f.write("a.txt\t1\t0\t" + sha1("a") + "\t-\n")
        self.assertFalse(self.plib.scan_complete(self.tsv))
        _, errors, out = self.scan()
        self.assertEqual(errors, [])
        self.assertIn("Resuming", out)
        self.assertEqual(self.paths(self.tsv), ["Sub/b.txt", "a.txt"])
        self.assertEqual([c[2] for c in self.cli_calls("filesystem", "list")], ["/my-files/Sub"])

    def test_scan_complete_without_a_tsv_is_false(self):
        self.assertFalse(self.plib.scan_complete(self.tsv))

    def test_recent_scan_suggests_reuse(self):
        self.drive({"/my-files/a.txt": "a"})
        self.scan()
        self.age(10)
        _, _, out = self.scan()
        self.assertIn("10 minutes old", out)
        self.assertIn("--reuse-scan", out)

    def test_old_scan_says_nothing(self):
        self.drive({"/my-files/a.txt": "a"})
        self.scan()
        self.age(self.plib.REUSE_HINT_MINUTES + 5)
        _, _, out = self.scan()
        self.assertNotIn("--reuse-scan", out)

    def test_incomplete_tsv_is_never_suggested(self):
        self.drive({"/my-files/a.txt": "a"})
        with open(self.tsv + ".state", "w") as f:
            f.write("")
        with open(self.tsv, "w") as f:
            f.write("")
        self.age(1)
        os.utime(self.tsv + ".state", (0, 0))     # too old to resume
        _, _, out = self.scan()
        self.assertNotIn("--reuse-scan", out)

    def test_ctrl_c_during_the_countdown_stops_before_scanning(self):
        self.drive({"/my-files/a.txt": "a"})
        self.scan()
        self.age(5)
        before = read(self.tsv)
        calls = len(self.cli_calls("filesystem", "list"))
        self.plib.REUSE_HINT_WAIT = 3
        with mock.patch.object(self.plib.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit) as cm:
                self.scan(tty=True)
        self.assertIn("--reuse-scan", str(cm.exception.code))
        self.assertEqual(read(self.tsv), before)
        self.assertEqual(len(self.cli_calls("filesystem", "list")), calls)

    def test_countdown_only_in_a_terminal(self):
        self.drive({"/my-files/a.txt": "a"})
        self.scan()
        self.age(5)
        self.plib.REUSE_HINT_WAIT = 3
        with mock.patch.object(self.plib.time, "sleep") as sleep:
            self.scan(tty=False)
            self.assertEqual(sleep.call_count, 0)
            self.scan(tty=True)
            self.assertGreaterEqual(sleep.call_count, 3)


if __name__ == "__main__":
    unittest.main()
