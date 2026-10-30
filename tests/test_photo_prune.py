# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
Unit tests for photo-prune.py: where its settings come from (the backup
folder from the photo-backup profile of the same name, its own optional
[photo-prune NAME] section, the command line) and end to end runs against a
fake proton-drive that records `filesystem trash` calls. The configuration
file itself is tested in test_protonlib.py.

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
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ScriptTestCase  # noqa: E402

FAKE_TRASH_CLI = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Fake proton-drive answering `filesystem trash /photos/UID`. $FAKE_STATE:
    {"fail_trash": ["u9"]}. Every call is appended to $FAKE_STATE.calls."""
    import json, os, sys
    st = json.load(open(os.environ["FAKE_STATE"], encoding="utf-8"))
    args = sys.argv[1:]
    with open(os.environ["FAKE_STATE"] + ".calls", "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\\n")
    if args[:2] == ["filesystem", "trash"]:
        if args[2].rsplit("/", 1)[1] in st.get("fail_trash", []):
            print("trash failed", file=sys.stderr); sys.exit(1)
        sys.exit(0)
    print("unknown: " + " ".join(args), file=sys.stderr); sys.exit(2)
''')


class PhotoPruneTestCase(ScriptTestCase):
    """Loads protonlib and photo-prune.py fresh, with HOME in a temp folder."""

    SCRIPT = ("photo_prune", "photo-prune.py")

    def setUp(self):
        super().setUp()
        self.pp = self.mod
        self.dest = self.mkdir("Disk", "proton-photos")

    def args(self, *argv):
        with mock.patch.object(sys, "argv", ["photo-prune.py", *argv]):
            return self.pp.parse_args()

    def resolve(self, *argv):
        return self.pp.resolve_settings(
            self.args(*argv, "--from-missing", "--config", self.config))

    def backup_profile(self, extra=""):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            albums = yes
            """ + textwrap.dedent(extra))


# ---------------------------------------------------------------------------
# 1. Resolving the settings of a run
# ---------------------------------------------------------------------------

class ResolveSettingsTest(PhotoPruneTestCase):

    def test_backup_folder_comes_from_the_photo_backup_profile(self):
        self.backup_profile()
        a = self.resolve("ssd")
        self.assertEqual(a.dest, self.dest)
        self.assertEqual(a.timeout, 120)
        self.assertEqual(a.where, f"profile [photo-backup ssd] in {self.config}")

    def test_single_photo_backup_profile_is_used_without_naming_it(self):
        self.backup_profile()
        self.assertEqual(self.resolve().dest, self.dest)

    def test_own_section_sets_the_timeout(self):
        self.backup_profile("""
            [photo-prune ssd]
            timeout = 30
            """)
        a = self.resolve()
        self.assertEqual((a.dest, a.timeout), (self.dest, 30))

    def test_default_section_applies(self):
        self.backup_profile("""
            [DEFAULT]
            timeout = 45
            """)
        self.assertEqual(self.resolve("ssd").timeout, 45)

    def test_command_line_timeout_wins(self):
        self.backup_profile("""
            [photo-prune ssd]
            timeout = 30
            """)
        self.assertEqual(self.resolve("ssd", "--timeout", "5").timeout, 5)

    def test_invalid_timeout_is_an_error(self):
        self.backup_profile("""
            [photo-prune ssd]
            timeout = 0
            """)
        self.assertExits(lambda: self.resolve("ssd"), "[photo-backup ssd]",
                         "timeout must be a number, 1 or more")

    def test_own_section_without_a_photo_backup_profile_is_an_error(self):
        self.write_config("""
            [photo-prune ssd]
            timeout = 30
            """)
        self.assertExits(lambda: self.resolve("ssd"), "neither a photo-backup profile",
                         "./photo-backup.py --setup ssd")
        self.assertExits(lambda: self.resolve(), "no photo-backup profile",
                         "./photo-backup.py --setup NAME")

    def test_several_photo_backup_profiles_need_a_name(self):
        other = self.mkdir("Other")
        self.backup_profile(f"""
            [photo-backup other]
            dest = {other}
            """)
        self.assertExits(lambda: self.resolve(), "several photo-backup profiles",
                         "ssd", "other")
        self.assertEqual(self.resolve("other").dest, other)

    def test_photo_backup_profile_without_dest_is_an_error(self):
        self.write_config("""
            [photo-backup ssd]
            albums = yes
            """)
        self.assertExits(lambda: self.resolve("ssd"), "has no dest",
                         "./photo-backup.py --setup ssd")

    def test_unplugged_disk_is_an_error(self):
        gone = os.path.join(self.tmp, "Unplugged", "photos")
        self.write_config(f"""
            [photo-backup ssd]
            dest = {gone}
            """)
        self.assertExits(lambda: self.resolve(), gone, "Is the disk plugged in?")

    def test_folder_path_works_without_a_profile(self):
        a = self.resolve(self.dest)
        self.assertEqual(a.dest, self.dest)
        self.assertEqual(a.where, "command line")

    def test_folder_path_that_does_not_exist_is_an_error(self):
        gone = os.path.join(self.tmp, "Gone")
        self.assertExits(lambda: self.resolve(gone), gone, "doesn't exist")

    def test_old_format_section_says_how_to_rename_it(self):
        self.write_config(f"""
            [ssd]
            dest = {self.dest}
            """)
        self.assertExits(lambda: self.resolve("ssd"), "old format", "[photo-backup ssd]")

    def test_a_mode_is_required(self):
        self.backup_profile()
        with mock.patch.object(sys, "argv", ["photo-prune.py", "--config", self.config]):
            self.assertExits(self.pp.parse_args, "--from-list", "--from-missing")

    def test_own_section_does_not_leak_into_photo_backup(self):
        self.backup_profile("""
            [photo-prune ssd]
            timeout = 30
            """)
        cfg = self.plib.read_config(self.config)
        self.assertEqual(self.plib.load_settings(cfg, "photo-backup", "ssd")["timeout"], "300")
        self.assertNotIn("albums", self.plib.load_settings(cfg, "photo-prune", "ssd"))


# ---------------------------------------------------------------------------
# 2. End to end, with a fake proton-drive
# ---------------------------------------------------------------------------

class EndToEndTest(PhotoPruneTestCase):

    def setUp(self):
        super().setUp()
        self.cli = self.write(os.path.join(self.tmp, "bin", "proton-drive"), FAKE_TRASH_CLI)
        os.chmod(self.cli, 0o755)
        self.state = os.path.join(self.tmp, "state.json")
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({}, f)
        env = mock.patch.dict(os.environ, {"FAKE_STATE": self.state})
        env.start()
        self.addCleanup(env.stop)
        self.kept = self.write(os.path.join(self.dest, "2023", "kept.jpg"), "kept")
        self.deleted = os.path.join(self.dest, "2023", "deleted.jpg")   # gone from disk
        self.map({"u1": self.kept, "u2": self.deleted})
        self.albums([{"uid": "al1", "name": "Trip", "photos": ["u1", "u2"]}])
        self.backup_profile()

    def map(self, m):
        self.write(os.path.join(self.dest, ".proton-photo-map.json"), json.dumps(m))

    def albums(self, a):
        self.write(os.path.join(self.dest, ".proton-albums.json"), json.dumps(a))

    def run_main(self, *argv):
        with mock.patch.object(sys, "argv", ["photo-prune.py", *argv, "--config", self.config,
                                             "--cli", self.cli]), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = self.pp.main()
        return rc, out.getvalue()

    def trash_calls(self):
        try:
            with open(self.state + ".calls", encoding="utf-8") as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []

    def test_dry_run_lists_without_trashing(self):
        rc, out = self.run_main("--from-missing", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn(f"Backup : {self.dest}   (profile [photo-backup ssd]", out)
        self.assertIn("2023/deleted.jpg", out)
        self.assertNotIn("kept.jpg", out)
        self.assertEqual(self.trash_calls(), [])

    def test_without_apply_nothing_is_trashed(self):
        rc, out = self.run_main("--from-missing")
        self.assertEqual(rc, 0)
        self.assertIn("add --apply", out)
        self.assertEqual(self.trash_calls(), [])

    def test_apply_trashes_only_what_is_missing_from_disk(self):
        rc, _ = self.run_main("--from-missing", "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(self.trash_calls(), [["filesystem", "trash", "/photos/u2"]])
        with open(os.path.join(self.dest, "prune-state.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"u2": "done"})
        self.assertTrue(os.path.exists(self.kept))

    def test_apply_again_does_not_trash_twice(self):
        self.run_main("--from-missing", "--apply")
        self.run_main("--from-missing", "--apply")
        self.assertEqual(len(self.trash_calls()), 1)

    def deleted_list(self, *rows):
        self.write(os.path.join(self.dest, "photos-deleted-locally.txt"),
                   "".join("\t".join(r) + "\n" for r in rows))

    def test_photos_listed_by_photo_backup_are_trashed_too(self):
        # u3 was deleted before the map remembered it: only the list knows it
        self.deleted_list(("u3", "old.jpg", "2019"), ("u2", "deleted.jpg", "2023"))
        rc, out = self.run_main("--from-missing", "--dry-run")
        self.assertIn("To trash on Proton    : 2", out)
        self.assertIn("2019/old.jpg", out)
        rc, _ = self.run_main("--from-missing", "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(c[2] for c in self.trash_calls()), ["/photos/u2", "/photos/u3"])

    def test_listed_photo_back_in_the_backup_is_left_alone(self):
        self.deleted_list(("u1", "kept.jpg", "2023"))     # downloaded again since
        rc, _ = self.run_main("--from-missing", "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(self.trash_calls(), [["filesystem", "trash", "/photos/u2"]])

    def test_refuses_to_empty_an_album(self):
        self.albums([{"uid": "al1", "name": "Trip", "photos": ["u2"]}])
        rc, out = self.run_main("--from-missing", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("left EMPTY", out)
        self.assertEqual(self.trash_calls(), [])

    def test_failed_trash_is_recorded_and_exits_one(self):
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"fail_trash": ["u2"]}, f)
        rc, _ = self.run_main("--from-missing", "--apply")
        self.assertEqual(rc, 1)
        with open(os.path.join(self.dest, "prune-state.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["u2"].startswith("error"))

    def test_from_list_deletes_the_local_file_and_trashes_it(self):
        self.write(os.path.join(self.dest, "dups.txt"), "delete 2023/kept.jpg\n")
        self.albums([{"uid": "al1", "name": "Trip", "photos": ["u1", "u2", "u3"]}])
        rc, _ = self.run_main("ssd", "--from-list", "dups.txt", "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(self.trash_calls(), [["filesystem", "trash", "/photos/u1"]])
        self.assertFalse(os.path.exists(self.kept))

    def test_profile_timeout_reaches_the_cli_call(self):
        self.backup_profile("""
            [photo-prune ssd]
            timeout = 33
            """)
        seen = []

        def fake_run_cli(cli, cmd, timeout, capture=True):
            seen.append((cmd, timeout))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(self.plib, "run_cli", fake_run_cli):
            rc, _ = self.run_main("--from-missing", "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [(["filesystem", "trash", "/photos/u2"], 33)])

    def test_missing_map_says_to_run_photo_backup(self):
        os.remove(os.path.join(self.dest, ".proton-photo-map.json"))
        with mock.patch.object(sys, "argv", ["photo-prune.py", "--from-missing",
                                             "--config", self.config, "--cli", self.cli]):
            self.assertExits(self.pp.main, ".proton-photo-map.json is missing",
                             "photo-backup.py")


if __name__ == "__main__":
    unittest.main()
