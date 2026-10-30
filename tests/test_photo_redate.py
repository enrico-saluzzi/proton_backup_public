# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
Unit tests for photo-redate.py: where its settings come from (the backup
folder from the photo-backup profile of the same name, its own optional
[photo-redate NAME] section with the album exclusions, the command line),
the --setup questions, and the plan step end to end. The plan reads only the
files photo-backup.py leaves in the backup folder, so it needs no CLI; the
--apply step (uploads, exiftool) isn't exercised here.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Every test runs with HOME pointing to a temporary folder, and the modules
are loaded fresh inside it, so nothing touches the real ~/drive-sync-logs,
~/.config/proton-backup or the Proton account.
"""

import contextlib
import io
import json
import os
import sys
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ScriptTestCase  # noqa: E402


class PhotoRedateTestCase(ScriptTestCase):
    """Loads protonlib and photo-redate.py fresh, with HOME in a temp folder."""

    SCRIPT = ("photo_redate", "photo-redate.py")

    def setUp(self):
        super().setUp()
        self.pr = self.mod
        self.dest = self.mkdir("Disk", "proton-photos")
        self.excl = self.write(os.path.join(self.tmp, "excluded.txt"),
                               "# albums with old photos on purpose\n\n2023 - Christmas\n")

    def args(self, *argv):
        with mock.patch.object(sys, "argv", ["photo-redate.py", *argv]):
            return self.pr.parse_args()

    def resolve(self, *argv):
        return self.pr.resolve_settings(self.args(*argv, "--config", self.config))

    def backup_profile(self, extra=""):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            albums = yes
            """ + textwrap.dedent(extra))


# ---------------------------------------------------------------------------
# 1. Resolving the settings of a run
# ---------------------------------------------------------------------------

class ResolveSettingsTest(PhotoRedateTestCase):

    def test_backup_folder_comes_from_the_photo_backup_profile(self):
        self.backup_profile()
        a = self.resolve("ssd")
        self.assertEqual(a.profile, "ssd")
        self.assertEqual(a.dest, self.dest)
        self.assertEqual((a.wait, a.timeout), (20, 600))
        self.assertEqual(a.excluded, set())
        self.assertEqual(a.work, os.path.join(self.dest, "_reupload"))
        self.assertEqual(a.where, f"profile [photo-backup ssd] in {self.config}")

    def test_single_photo_backup_profile_is_used_without_naming_it(self):
        self.backup_profile()
        self.assertEqual(self.resolve().profile, "ssd")

    def test_own_section_adds_its_settings_and_is_named_in_where(self):
        self.backup_profile(f"""
            [photo-redate ssd]
            exclude = 2001 - Old, 2002 - Older
            exclude_file = {self.excl}
            wait = 5
            timeout = 90
            """)
        a = self.resolve()
        self.assertEqual(a.dest, self.dest)
        self.assertEqual(a.excluded, {"2001 - Old", "2002 - Older", "2023 - Christmas"})
        self.assertEqual((a.wait, a.timeout), (5, 90))
        self.assertEqual(a.where, f"profile [photo-backup ssd] and [photo-redate ssd] "
                                  f"in {self.config}")

    def test_exclude_file_skips_comments_and_blank_lines(self):
        self.backup_profile(f"""
            [photo-redate ssd]
            exclude_file = {self.excl}
            """)
        self.assertEqual(self.resolve().excluded, {"2023 - Christmas"})

    def test_configured_exclude_file_that_does_not_exist_is_an_error(self):
        gone = os.path.join(self.tmp, "gone.txt")
        self.backup_profile(f"""
            [photo-redate ssd]
            exclude_file = {gone}
            """)
        self.assertExits(lambda: self.resolve(), "exclude_file", gone, "doesn't exist")

    # a folder must not pass for the exclude file
    def test_exclude_file_that_is_a_folder_is_a_clean_error(self):
        folder = self.mkdir("NotAFile")
        self.backup_profile(f"""
            [photo-redate ssd]
            exclude_file = {folder}
            """)
        self.assertExits(lambda: self.resolve(), "exclude_file", folder)

    def test_command_line_overrides_the_own_section(self):
        other = self.write(os.path.join(self.tmp, "other.txt"), "1999 - Party\n")
        self.backup_profile(f"""
            [photo-redate ssd]
            exclude = 2001 - Old
            exclude_file = {self.excl}
            wait = 5
            timeout = 90
            """)
        a = self.resolve("--exclude", "2010 - Snow", "--exclude-file", other,
                         "--wait", "0", "--timeout", "30")
        self.assertEqual(a.excluded, {"2010 - Snow", "1999 - Party"})
        self.assertEqual((a.wait, a.timeout), (0, 30))

    def test_command_line_exclude_adds_to_the_profile_exclude_file(self):
        self.backup_profile(f"""
            [photo-redate ssd]
            exclude_file = {self.excl}
            """)
        self.assertEqual(self.resolve("--exclude", "2010 - Snow").excluded,
                         {"2010 - Snow", "2023 - Christmas"})

    def test_default_section_applies(self):
        self.backup_profile("""
            [DEFAULT]
            wait = 3
            """)
        self.assertEqual(self.resolve().wait, 3)

    def test_invalid_numbers_are_errors(self):
        for key, bad, needle in (("wait", "-1", "wait must be a number, 0 or more"),
                                 ("timeout", "0", "timeout must be a number, 1 or more"),
                                 ("wait", "soon", "wait must be a number")):
            with self.subTest(key=key, bad=bad):
                self.backup_profile(f"""
                    [photo-redate ssd]
                    {key} = {bad}
                    """)
                self.assertExits(lambda: self.resolve(), "[photo-redate ssd]", needle)

    def test_work_folder_option_is_expanded(self):
        self.backup_profile()
        a = self.resolve("--work", "~/redate-work")
        self.assertEqual(a.work, os.path.join(self.home, "redate-work"))

    def test_own_section_without_a_photo_backup_profile_is_an_error(self):
        self.write_config(f"""
            [photo-redate ssd]
            exclude_file = {self.excl}
            """)
        self.assertExits(lambda: self.resolve("ssd"), "neither a photo-backup profile",
                         "./photo-backup.py --setup ssd")
        self.assertExits(lambda: self.resolve(), "no photo-backup profile")

    def test_several_photo_backup_profiles_need_a_name(self):
        other = self.mkdir("Other")
        self.backup_profile(f"""
            [photo-backup other]
            dest = {other}
            """)
        self.assertExits(lambda: self.resolve(), "several photo-backup profiles")
        self.assertEqual(self.resolve("other").dest, other)

    def test_photo_backup_profile_without_dest_is_an_error(self):
        self.write_config("""
            [photo-backup ssd]
            albums = yes
            """)
        self.assertExits(lambda: self.resolve(), "has no dest", "./photo-backup.py --setup ssd")

    def test_unplugged_disk_is_an_error(self):
        gone = os.path.join(self.tmp, "Unplugged")
        self.write_config(f"""
            [photo-backup ssd]
            dest = {gone}
            """)
        self.assertExits(lambda: self.resolve(), gone, "Is the disk plugged in?")

    def test_folder_path_works_without_a_profile(self):
        a = self.resolve(self.dest, "--exclude-file", self.excl)
        self.assertIsNone(a.profile)
        self.assertEqual(a.dest, self.dest)
        self.assertEqual(a.excluded, {"2023 - Christmas"})
        self.assertEqual(a.where, "defaults and command line")

    def test_old_format_section_says_how_to_rename_it(self):
        self.write_config(f"""
            [ssd]
            dest = {self.dest}
            """)
        self.assertExits(lambda: self.resolve("ssd"), "old format", "[photo-backup ssd]")

    def test_own_section_does_not_leak_into_other_scripts(self):
        self.backup_profile("""
            [photo-redate ssd]
            timeout = 90
            """)
        cfg = self.plib.read_config(self.config)
        self.assertEqual(self.plib.load_settings(cfg, "photo-backup", "ssd")["timeout"], "300")
        self.assertEqual(self.plib.load_settings(cfg, "photo-prune", "ssd")["timeout"], "120")


# ---------------------------------------------------------------------------
# 2. The --setup questions
# ---------------------------------------------------------------------------

class SetupTest(PhotoRedateTestCase):

    def run_setup(self, answers, *argv, tty=True):
        with self.answering(answers, tty=tty) as prompts, \
                mock.patch.object(sys, "argv",
                                  ["photo-redate.py", "--setup", *argv,
                                   "--config", self.config]), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = self.pr.main()
        return rc, prompts

    def own(self, name="ssd"):
        cfg = self.plib.read_config(self.config)
        return {k: v for k, v in cfg.items(f"photo-redate {name}", raw=True)
                if k not in cfg.defaults()}

    def test_new_section_with_explicit_answers(self):
        self.backup_profile()
        rc, _ = self.run_setup(["2001 - Old", self.excl, "5", "90"], "ssd")
        self.assertEqual(rc, 0)
        self.assertEqual(self.own(), {"exclude": "2001 - Old", "exclude_file": self.excl,
                                      "wait": "5", "timeout": "90"})

    def test_enter_keeps_the_defaults_and_none(self):
        self.backup_profile()
        self.run_setup(["", "", "", ""], "ssd")
        self.assertEqual(self.own(), {"exclude": "", "exclude_file": "",
                                      "wait": "20", "timeout": "600"})

    def test_none_clears_a_value(self):
        self.backup_profile(f"""
            [photo-redate ssd]
            exclude = 2001 - Old
            exclude_file = {self.excl}
            """)
        self.run_setup(["none", "none", "", ""], "ssd")
        self.assertEqual((self.own()["exclude"], self.own()["exclude_file"]), ("", ""))

    def test_missing_exclude_file_is_asked_again(self):
        self.backup_profile()
        gone = os.path.join(self.tmp, "gone.txt")
        _, prompts = self.run_setup(["", gone, self.excl, "", ""], "ssd")
        self.assertEqual(sum(1 for p in prompts if p.strip().startswith("exclude_file")), 2)
        self.assertEqual(self.own()["exclude_file"], self.excl)

    def test_invalid_numbers_are_asked_again(self):
        self.backup_profile()
        self.run_setup(["", "", "-1", "0", "0", "60"], "ssd")
        self.assertEqual((self.own()["wait"], self.own()["timeout"]), ("0", "60"))

    def test_current_values_are_offered_as_defaults(self):
        self.backup_profile(f"""
            [photo-redate ssd]
            exclude_file = {self.excl}
            wait = 7
            """)
        _, prompts = self.run_setup(["", "", "", ""], "ssd")
        self.assertTrue(any(self.excl in p for p in prompts))
        self.assertTrue(any(p.strip().startswith("wait") and "(7)" in p for p in prompts))

    def test_photo_backup_profile_is_kept_and_saved_section_resolves(self):
        self.backup_profile()
        self.run_setup(["", self.excl, "1", ""], "ssd")
        cfg = self.plib.read_config(self.config)
        self.assertEqual(cfg.get("photo-backup ssd", "dest"), self.dest)
        self.assertEqual(cfg.get("photo-backup ssd", "albums"), "yes")
        a = self.resolve()
        self.assertEqual((a.dest, a.wait, a.excluded), (self.dest, 1, {"2023 - Christmas"}))

    def test_refuses_without_a_terminal(self):
        self.backup_profile()
        with self.answering([], tty=False), \
                mock.patch.object(sys, "argv", ["photo-redate.py", "--setup", "ssd",
                                                "--config", self.config]):
            self.assertExits(self.pr.main, "run it in a terminal")
        self.assertFalse(self.plib.read_config(self.config).has_section("photo-redate ssd"))

    def test_ctrl_c_writes_nothing(self):
        self.backup_profile()
        with self.answering(["", KeyboardInterrupt()]), \
                mock.patch.object(sys, "argv", ["photo-redate.py", "--setup", "ssd",
                                                "--config", self.config]):
            self.assertExits(self.pr.main, "nothing written")
        self.assertFalse(self.plib.read_config(self.config).has_section("photo-redate ssd"))


# ---------------------------------------------------------------------------
# 3. The plan, end to end
# ---------------------------------------------------------------------------

class PlanTest(PhotoRedateTestCase):

    def setUp(self):
        super().setUp()
        photos = {
            "u1": ("a.jpg", "2003-07-10T09:00:00Z"),   # right year
            "u2": ("b.jpg", "2019-01-05T10:00:00Z"),   # scanned later
            "u3": ("c.jpg", "2015-03-03T10:00:00Z"),   # in the excluded album
            "u4": ("d.jpg", "2021-02-02T10:00:00Z"),   # album with no right-year photo
        }
        inv = [{"nodeUid": u, "name": {"ok": True, "value": n}, "photo": {"captureTime": t}}
               for u, (n, t) in photos.items()]
        self.write(os.path.join(self.dest, ".proton-photo-inventory.json"), json.dumps(inv))
        self.write(os.path.join(self.dest, ".proton-photo-map.json"), json.dumps(
            {u: os.path.join(self.dest, t[:4], n) for u, (n, t) in photos.items()}))
        self.write(os.path.join(self.dest, ".proton-albums.json"), json.dumps([
            {"uid": "al1", "name": "2003 - Holiday", "photos": ["u1", "u2"]},
            {"uid": "al2", "name": "2023 - Christmas", "photos": ["u3"]},
            {"uid": "al3", "name": "1998 - School", "photos": ["u4"]},
            {"uid": "al4", "name": "Friends", "photos": ["u2"]},
        ]))

    def run_main(self, *argv):
        with mock.patch.object(sys, "argv", ["photo-redate.py", *argv, "--config", self.config,
                                             "--cli", os.path.join(self.tmp, "no-such-cli")]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = self.pr.main()
        return rc, out.getvalue()

    def plan(self):
        with open(os.path.join(self.dest, "redate-plan.json"), encoding="utf-8") as f:
            return {p["uid"]: p for p in json.load(f)}

    def test_profile_exclusions_are_applied_to_the_plan(self):
        self.backup_profile(f"""
            [photo-redate ssd]
            exclude_file = {self.excl}
            """)
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn(f"Backup : {self.dest}", out)
        plan = self.plan()
        self.assertEqual(set(plan), {"u2", "u4"})
        self.assertEqual(plan["u2"]["new_date"], "2003-07-10T09:00:00Z")
        self.assertEqual(plan["u2"]["album"], "2003 - Holiday")
        self.assertEqual(plan["u4"]["new_date"], "1998-01-01T12:00:00Z")
        self.assertEqual(plan["u2"]["local_file"], os.path.join(self.dest, "2019", "b.jpg"))

    def test_without_exclusions_every_year_album_is_planned(self):
        self.backup_profile()
        self.run_main("ssd")
        self.assertEqual(set(self.plan()), {"u2", "u3", "u4"})

    def test_command_line_exclude_on_a_folder_path(self):
        rc, _ = self.run_main(self.dest, "--exclude", "1998 - School,2023 - Christmas")
        self.assertEqual(rc, 0)
        self.assertEqual(set(self.plan()), {"u2"})

    def test_missing_backup_files_say_to_run_photo_backup(self):
        os.remove(os.path.join(self.dest, ".proton-albums.json"))
        self.backup_profile()
        with mock.patch.object(sys, "argv", ["photo-redate.py", "--config", self.config]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertExits(self.pr.main, ".proton-albums.json is missing",
                             "photo-backup.py --albums")


if __name__ == "__main__":
    unittest.main()
