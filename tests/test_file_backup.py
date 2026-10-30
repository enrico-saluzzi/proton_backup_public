# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
Unit tests for file-backup.py: its settings (profiles, overrides), the
--setup questions, and end to end backups against a fake proton-drive CLI
that lists and downloads files from a JSON state. The configuration file
itself is tested in test_protonlib.py.

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
import time
import unicodedata
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ScriptTestCase, read  # noqa: E402


class FileBackupTestCase(ScriptTestCase):
    """Loads protonlib and file-backup.py fresh, with HOME in a temp folder."""

    SCRIPT = ("file_backup", "file-backup.py")

    def setUp(self):
        super().setUp()
        self.fb = self.mod

    def args(self, *argv):
        with mock.patch.object(sys, "argv", ["file-backup.py", *argv]):
            return self.fb.parse_args()

    def resolve(self, *argv):
        return self.fb.resolve_settings(self.args(*argv, "--config", self.config))


# ---------------------------------------------------------------------------
# 1. Resolving the settings of a run
# ---------------------------------------------------------------------------

class ResolveSettingsTest(FileBackupTestCase):

    def setUp(self):
        super().setUp()
        self.dest = self.mkdir("Disk", "proton-files")

    def test_profile_loads_its_section(self):
        self.write_config(f"""
            [file-backup ssd]
            dest = {self.dest}
            proton_root = /my-files/Work
            verify = no
            workers = 5
            batch = 7
            timeout = 60
            """)
        a = self.resolve("ssd")
        self.assertEqual(a.profile, "ssd")
        self.assertEqual(a.dest, self.dest)
        self.assertEqual(a.root, "/my-files/Work")
        self.assertIs(a.verify, False)
        self.assertEqual((a.workers, a.batch, a.timeout), (5, 7, 60))
        self.assertEqual(a.where, f"profile [file-backup ssd] in {self.config}")

    def test_unset_keys_take_the_defaults_and_verify_is_on(self):
        self.write_config(f"""
            [file-backup ssd]
            dest = {self.dest}
            """)
        a = self.resolve("ssd")
        self.assertEqual(a.root, "/my-files")
        self.assertIs(a.verify, True)
        self.assertEqual((a.workers, a.batch, a.timeout), (3, 20, 1800))

    def test_no_target_uses_the_only_file_backup_profile(self):
        self.write_config(f"""
            [file-backup ssd]
            dest = {self.dest}

            [drive-compare dropbox]
            folder = /data
            """)
        self.assertEqual(self.resolve().profile, "ssd")

    def test_no_target_and_no_profile_is_an_error(self):
        self.write_config("""
            [drive-compare dropbox]
            folder = /data
            """)
        self.assertExits(lambda: self.resolve(), "no file-backup profile",
                         "./file-backup.py --setup NAME")

    def test_no_target_with_several_profiles_is_an_error(self):
        self.write_config(f"""
            [file-backup a]
            dest = {self.dest}

            [file-backup b]
            dest = {self.dest}
            """)
        self.assertExits(lambda: self.resolve(), "several file-backup profiles", "a, b")

    def test_folder_path_uses_the_defaults(self):
        a = self.resolve(self.dest)
        self.assertIsNone(a.profile)
        self.assertEqual(a.dest, self.dest)
        self.assertIs(a.verify, True)
        self.assertEqual(a.where, "defaults and command line")

    def test_folder_path_takes_the_default_section(self):
        self.write_config("""
            [DEFAULT]
            verify = no
            batch = 4
            """)
        a = self.resolve(self.dest)
        self.assertIs(a.verify, False)
        self.assertEqual(a.batch, 4)

    def test_folder_path_that_does_not_exist_yet_is_accepted(self):
        new = os.path.join(self.tmp, "new-backup")
        a = self.resolve(new)
        self.assertEqual(a.dest, new)
        self.assertFalse(os.path.exists(new))      # main() creates it

    def test_tilde_dest_is_expanded(self):
        self.write_config("""
            [file-backup home]
            dest = ~/Backup
            """)
        os.makedirs(os.path.join(self.home, "Backup"))
        self.assertEqual(self.resolve("home").dest, os.path.join(self.home, "Backup"))

    def test_command_line_overrides_the_profile(self):
        self.write_config(f"""
            [file-backup ssd]
            dest = {self.dest}
            verify = yes
            workers = 3
            batch = 20
            timeout = 1800
            """)
        a = self.resolve("ssd", "--root", "/my-files/Work", "--no-verify",
                         "--workers", "1", "--batch", "2", "--timeout", "9")
        self.assertEqual(a.root, "/my-files/Work")
        self.assertIs(a.verify, False)
        self.assertEqual((a.workers, a.batch, a.timeout), (1, 2, 9))

    def test_verify_option_turns_verification_back_on(self):
        self.write_config(f"""
            [file-backup ssd]
            dest = {self.dest}
            verify = no
            """)
        self.assertIs(self.resolve("ssd", "--verify").verify, True)

    def test_verify_and_no_verify_together_are_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.args("--verify", "--no-verify")
        self.assertEqual(cm.exception.code, 2)

    def test_invalid_numbers_in_the_profile_are_errors(self):
        for key, value, low in (("workers", "0", 1), ("batch", "-2", 1), ("timeout", "abc", 1)):
            with self.subTest(key=key):
                self.write_config(f"""
                    [file-backup ssd]
                    dest = {self.dest}
                    {key} = {value}
                    """)
                self.assertExits(lambda: self.resolve("ssd"), "profile [file-backup ssd]",
                                 f"{key} must be a number, {low} or more")

    def test_invalid_numbers_on_the_command_line_are_errors(self):
        self.assertExits(lambda: self.resolve(self.dest, "--batch", "0"),
                         "batch must be a number, 1 or more")

    def test_invalid_verify_value_is_an_error(self):
        self.write_config(f"""
            [file-backup ssd]
            dest = {self.dest}
            verify = always
            """)
        self.assertExits(lambda: self.resolve("ssd"), "verify = 'always'", "yes, no")

    def test_profile_without_dest_is_an_error(self):
        self.write_config("""
            [file-backup ssd]
            verify = yes
            """)
        self.assertExits(lambda: self.resolve("ssd"), "has no dest", "--setup ssd")

    def test_profile_dest_that_does_not_exist_is_an_error_and_is_not_created(self):
        missing = os.path.join(self.tmp, "Unplugged", "proton-files")
        self.write_config(f"""
            [file-backup ssd]
            dest = {missing}
            """)
        self.assertExits(lambda: self.resolve("ssd"), missing, "Is the disk plugged in?")
        self.assertFalse(os.path.exists(os.path.dirname(missing)))

    def test_old_format_section_is_reported(self):
        self.write_config(f"""
            [ssd]
            dest = {self.dest}
            """)
        self.assertExits(lambda: self.resolve("ssd"), "old format", "[file-backup ssd]")

    def test_unknown_profile_suggests_setup(self):
        self.assertExits(lambda: self.resolve("nope"),
                         "'nope' is neither a file-backup profile",
                         "./file-backup.py --setup nope")

    def test_drive_compare_profile_is_not_a_file_backup_profile(self):
        self.write_config("""
            [drive-compare ssd]
            folder = /data
            """)
        self.assertExits(lambda: self.resolve("ssd"), "neither a file-backup profile")

    def test_config_option_reads_a_file_in_the_current_folder(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.mkdir("work"))
        self.write_config(f"""
            [file-backup here]
            dest = {self.dest}
            batch = 3
            """, path=os.path.join(self.tmp, "work", "my-config.ini"))
        a = self.fb.resolve_settings(self.args("--config", "my-config.ini"))
        self.assertEqual(a.profile, "here")
        self.assertEqual(a.batch, 3)


# ---------------------------------------------------------------------------
# 2. --setup: the file-backup questions
# ---------------------------------------------------------------------------

class SetupTest(FileBackupTestCase):

    def setUp(self):
        super().setUp()
        self.dest = self.mkdir("Disk", "proton-files")

    def run_setup(self, answers, *argv, tty=True):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["file-backup.py", "--setup", *argv,
                                             "--config", self.config]), \
                self.answering(answers, tty=tty) as prompts, contextlib.redirect_stdout(out):
            rv = self.fb.main()
        return rv, out.getvalue(), prompts

    def saved(self, name="ssd"):
        return dict(self.plib.read_config(self.config).items(f"file-backup {name}"))

    def test_new_profile_with_explicit_answers(self):
        rv, out, _ = self.run_setup([self.dest, "/my-files/Work", "no", "yes", "/Disk/old",
                                     "4", "10", "600"], "ssd")
        self.assertEqual(rv, 0)
        self.assertEqual(self.saved(), {"dest": self.dest, "proton_root": "/my-files/Work",
                                        "verify": "no", "cleanup": "yes",
                                        "deleted_dir": "/Disk/old", "workers": "4",
                                        "batch": "10", "timeout": "600"})
        self.assertIn("Saved profile [file-backup ssd]", out)
        self.assertIn("(or ./file-backup.py ssd)", out)

    def test_enter_keeps_the_defaults(self):
        self.run_setup([self.dest] + [""] * 7, "ssd")
        self.assertEqual(self.saved(), {"dest": self.dest, "proton_root": "/my-files",
                                        "verify": "yes", "cleanup": "yes",
                                        "deleted_dir": "", "workers": "3",
                                        "batch": "20", "timeout": "1800"})

    def test_new_profile_offers_an_example_dest(self):
        _, _, prompts = self.run_setup([self.dest] + [""] * 7, "ssd")
        self.assertIn("(/Volumes/MyBackupDisk/proton-files)", prompts[0])
        self.assertIn("verify [yes/no] (yes)", prompts[2])
        self.assertIn("cleanup [yes/no] (yes)", prompts[3])

    def test_invalid_answers_are_asked_again(self):
        _, out, _ = self.run_setup([self.dest, "", "maybe", "no", "", "", "0", "x", "2", "", ""], "ssd")
        self.assertEqual(self.saved()["verify"], "no")
        self.assertEqual(self.saved()["workers"], "2")
        self.assertIn("answer one of: yes, no", out)
        self.assertEqual(out.count("a number, 1 or more"), 2)

    def test_missing_dest_can_be_kept_anyway(self):
        unplugged = "/Volumes/NotPluggedIn/proton-files"
        _, out, _ = self.run_setup([unplugged, "yes"] + [""] * 7, "ssd")
        self.assertEqual(self.saved()["dest"], unplugged)
        self.assertIn("doesn't exist", out)

    def test_missing_dest_is_asked_again_when_not_kept(self):
        self.run_setup(["/Volumes/NotPluggedIn", "no", self.dest] + [""] * 7, "ssd")
        self.assertEqual(self.saved()["dest"], self.dest)

    def test_current_values_are_offered_as_defaults(self):
        self.write_config(f"""
            [DEFAULT]
            verify = no

            [file-backup ssd]
            dest = {self.dest}
            batch = 9
            """)
        _, _, prompts = self.run_setup([""] * 8, "ssd")
        self.assertIn(f"({self.dest})", prompts[0])
        self.assertIn("(no)", prompts[2])
        self.assertIn("(9)", prompts[6])

    def test_saved_profile_resolves(self):
        self.run_setup([self.dest, "", "no", "", "", "", "5", ""], "ssd")
        a = self.resolve()
        self.assertEqual(a.dest, self.dest)
        self.assertIs(a.verify, False)
        self.assertEqual(a.batch, 5)

    def test_setup_keeps_drive_compare_profiles(self):
        self.write_config("""
            [drive-compare dropbox]
            folder = /data
            winner = ask
            """)
        self.run_setup([self.dest] + [""] * 7, "ssd")
        cfg = self.plib.read_config(self.config)
        self.assertEqual(cfg.get("drive-compare dropbox", "winner"), "ask")
        self.assertIn("file-backup ssd", cfg.sections())

    def test_refuses_without_a_terminal(self):
        self.assertExits(lambda: self.run_setup([], "ssd", tty=False), "run it in a terminal")
        self.assertFalse(os.path.exists(self.config))

    def test_ctrl_c_writes_nothing(self):
        self.assertExits(lambda: self.run_setup([self.dest, KeyboardInterrupt()], "ssd"),
                         "nothing written")
        self.assertFalse(os.path.exists(self.config))


# ---------------------------------------------------------------------------
# 3. End to end, against a fake proton-drive
# ---------------------------------------------------------------------------

class EndToEndCase(FileBackupTestCase):
    """A fake Proton Drive, a backup folder and a profile pointing to it."""

    def setUp(self):
        super().setUp()
        self.cli = self.fake_cli()
        self.dest = self.mkdir("Disk", "proton-files")
        self.logs = self.plib.LOG_DIR
        self.drive = {}
        self.fail = []
        # the scanner waits 0.2 s before every CLI call: not needed here
        real_scan = self.plib.scan_drive
        patch = mock.patch.object(self.plib, "scan_drive",
                                  lambda cli, root, **kw: real_scan(cli, root, **dict(kw, delay=0)))
        patch.start()
        self.addCleanup(patch.stop)

    def set_proton(self, files=None, fail=None):
        if files is not None:
            self.drive = {"/my-files/" + k: v for k, v in files.items()}
        if fail is not None:
            self.fail = ["/my-files/" + p for p in fail]
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"drive": self.drive, "fail_download": self.fail}, f, ensure_ascii=False)

    def profile(self, **settings):
        lines = [f"dest = {self.dest}"] + [f"{k} = {v}" for k, v in settings.items()]
        self.write_config("[file-backup ssd]\n" + "\n".join(lines) + "\n")

    def run_main(self, *argv):
        """Run main(); returns (exit code, terminal output)."""
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["file-backup.py", *argv, "--cli", self.cli,
                                             "--config", self.config]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rv = self.fb.main()
        return rv, out.getvalue()

    def backup(self, rel):
        return read(os.path.join(self.dest, rel))

    def downloads(self):
        return self.cli_calls("filesystem", "download")


class EndToEndTest(EndToEndCase):

    def test_first_backup_downloads_everything(self):
        self.profile()
        self.set_proton({"a.txt": "alpha", "Docs/b.txt": "beta", "Docs/Sub/c.txt": "gamma"})
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(self.backup("a.txt"), "alpha")
        self.assertEqual(self.backup("Docs/b.txt"), "beta")
        self.assertEqual(self.backup("Docs/Sub/c.txt"), "gamma")
        self.assertIn("Downloaded : 3", out)
        self.assertIn("Backup in sync.", out)
        self.assertIn("FROM   : profile [file-backup ssd]", out)

    def test_download_uses_remove_and_merge_strategies(self):
        self.profile()
        self.set_proton({"a.txt": "alpha"})
        self.run_main()
        self.assertEqual(self.downloads(),
                         [["filesystem", "download", "-f", "remove", "-d", "merge",
                           "/my-files/a.txt", self.dest]])

    def test_second_run_downloads_nothing(self):
        self.profile()
        self.set_proton({"a.txt": "alpha", "Docs/b.txt": "beta"})
        self.run_main()
        calls = len(self.downloads())
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertIn("Backup in sync: nothing to download.", out)
        self.assertEqual(len(self.downloads()), calls)

    def test_changed_remote_file_is_downloaded_again(self):
        self.profile()
        self.set_proton({"a.txt": "alpha", "b.txt": "beta"})
        self.run_main()
        self.set_proton({"a.txt": "alpha, longer now", "b.txt": "beta"})
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(self.backup("a.txt"), "alpha, longer now")
        self.assertIn("Different (re-download) .... 1", out)
        self.assertEqual(self.downloads()[-1][-2:], ["/my-files/a.txt", self.dest])

    def test_same_size_corruption_is_caught_when_verifying(self):
        self.profile()
        self.set_proton({"a.txt": "alpha"})
        self.run_main()
        self.write(os.path.join(self.dest, "a.txt"), "alphX")     # same size
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(self.backup("a.txt"), "alpha")
        self.assertIn("Different (re-download) .... 1", out)
        self.assertIn("Verified by content", out)

    def test_same_size_corruption_is_missed_with_no_verify(self):
        self.profile()
        self.set_proton({"a.txt": "alpha"})
        self.run_main()
        self.write(os.path.join(self.dest, "a.txt"), "alphX")
        rv, out = self.run_main("--no-verify")
        self.assertEqual(rv, 0)
        self.assertEqual(self.backup("a.txt"), "alphX")
        self.assertIn("nothing to download", out)
        self.assertIn("(no sha1 verification)", out)

    def test_verify_no_in_the_profile_skips_verification(self):
        self.profile(verify="no")
        self.set_proton({"a.txt": "alpha"})
        self.run_main()
        self.write(os.path.join(self.dest, "a.txt"), "alphX")
        _, out = self.run_main()
        self.assertEqual(self.backup("a.txt"), "alphX")
        self.assertNotIn("Verified by content", out)

    def test_files_only_in_the_backup_are_reported_and_kept_with_cleanup_no(self):
        self.profile(cleanup="no")
        self.set_proton({"a.txt": "alpha"})
        self.write(os.path.join(self.dest, "Old", "gone.txt"), "deleted on Proton")
        self.write(os.path.join(self.dest, ".DS_Store"), "x")
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertIn("Only in backup (not on Proton) 1", out)
        self.assertIn("Old/gone.txt", out)
        self.assertNotIn(".DS_Store", out.split("NOT touched")[-1])
        self.assertEqual(self.backup("Old/gone.txt"), "deleted on Proton")

    def test_dry_run_downloads_nothing(self):
        self.profile()
        self.set_proton({"a.txt": "alpha", "Docs/b.txt": "beta"})
        rv, out = self.run_main("--dry-run")
        self.assertEqual(rv, 0)
        self.assertIn("To download: 2 files", out)
        self.assertIn("(--dry-run: nothing downloaded)", out)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(os.listdir(self.dest), [])

    def test_failed_batch_is_listed_and_retried_on_the_next_run(self):
        self.profile()
        self.set_proton({"a.txt": "alpha", "Docs/b.txt": "beta", "Docs/c.txt": "gamma"},
                        fail=["Docs/b.txt"])
        rv, out = self.run_main()
        self.assertEqual(rv, 1)
        self.assertEqual(self.backup("a.txt"), "alpha")
        failed = read(os.path.join(self.logs, "files-not-downloaded.txt"))
        # the whole batch of Docs failed, and it's listed with the CLI's error
        self.assertEqual(sorted(l.split("\t")[0] for l in failed.splitlines()),
                         ["Docs/b.txt", "Docs/c.txt"])
        self.assertIn("network error", failed)
        self.assertIn("Failed     : 2", out)

        self.set_proton(fail=[])
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(self.backup("Docs/b.txt"), "beta")
        self.assertEqual(self.backup("Docs/c.txt"), "gamma")
        self.assertIn("Downloaded : 2", out)
        # only what was still missing was downloaded again
        self.assertEqual(sorted(p for c in self.downloads()[-1:] for p in c[6:-1]),
                         ["/my-files/Docs/b.txt", "/my-files/Docs/c.txt"])

    def test_batch_size_from_the_profile_is_respected(self):
        self.profile(batch=2)
        self.set_proton({f"Docs/f{i}.txt": str(i) for i in range(5)})
        self.run_main()
        sizes = [len(c) - 7 for c in self.downloads()]      # 6 words before, target after
        self.assertEqual(sizes, [2, 2, 1])

    def test_batch_option_overrides_the_profile(self):
        self.profile(batch=2)
        self.set_proton({f"f{i}.txt": str(i) for i in range(5)})
        self.run_main("--batch", "5")
        self.assertEqual(len(self.downloads()), 1)

    def test_each_download_call_targets_one_folder(self):
        self.profile()
        self.set_proton({"a.txt": "a", "Docs/b.txt": "b", "Docs/Sub/c.txt": "c"})
        self.run_main()
        targets = sorted(c[-1] for c in self.downloads())
        self.assertEqual(targets, sorted([self.dest, os.path.join(self.dest, "Docs"),
                                          os.path.join(self.dest, "Docs", "Sub")]))

    def test_nfd_names_and_forbidden_characters_stay_in_sync(self):
        nfd = unicodedata.normalize("NFD", "Café")
        self.profile()
        self.set_proton({f"{nfd}/notes.txt": "accent", "Docs/10:30 meeting.txt": "colon"})
        rv, _ = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(self.backup("Café/notes.txt"), "accent")
        self.assertEqual(self.backup("Docs/10_30 meeting.txt"), "colon")
        # the CLI gets the exact name Proton stored (NFD), not the NFC form
        sent = [p for c in self.downloads() for p in c[6:-1]]
        self.assertIn(f"/my-files/{nfd}/notes.txt", sent)
        rv, out = self.run_main()
        self.assertIn("nothing to download", out)
        self.assertIn("Only in backup (not on Proton) 0", out)

    def test_folder_path_creates_the_backup_folder(self):
        self.set_proton({"a.txt": "alpha"})
        new = os.path.join(self.tmp, "new-backup")
        rv, out = self.run_main(new)
        self.assertEqual(rv, 0)
        self.assertEqual(read(os.path.join(new, "a.txt")), "alpha")
        self.assertIn("FROM   : defaults and command line", out)

    def test_missing_profile_dest_stops_before_scanning(self):
        missing = os.path.join(self.tmp, "Unplugged")
        self.write_config(f"[file-backup ssd]\ndest = {missing}\n")
        self.set_proton({"a.txt": "alpha"})
        self.assertExits(lambda: self.run_main(), "Is the disk plugged in?")
        self.assertEqual(self.cli_calls("filesystem"), [])
        self.assertFalse(os.path.exists(missing))

    def test_root_from_the_profile_backs_up_a_subfolder(self):
        self.profile(proton_root="/my-files/Work")
        self.set_proton({"Work/a.txt": "work", "Home/b.txt": "home"})
        rv, _ = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(os.listdir(self.dest), ["a.txt"])
        self.assertTrue(os.path.exists(os.path.join(self.logs, "my-files-Work.tsv")))

    def test_reuse_scan_needs_a_previous_scan(self):
        self.profile()
        self.set_proton({"a.txt": "alpha"})
        self.assertExits(lambda: self.run_main("--reuse-scan"), "no complete scan")
        self.run_main()
        lists = len(self.cli_calls("filesystem", "list"))
        rv, out = self.run_main("--reuse-scan")
        self.assertEqual(rv, 0)
        self.assertIn("Scan reused", out)
        self.assertEqual(len(self.cli_calls("filesystem", "list")), lists)


# ---------------------------------------------------------------------------
# 4. Cleanup: the backup follows Proton (cleanup = yes)
# ---------------------------------------------------------------------------

class CleanupTest(EndToEndCase):

    # ten files that stay, so that a few removals are under the 10% limit
    STAY = {f"Keep/k{i}.txt": f"keep {i}" for i in range(10)}

    def path(self, *parts):
        return os.path.join(self.dest, *parts)

    def held(self, *parts):
        # deleted/files next to the backup folder
        return os.path.join(os.path.dirname(self.dest), "deleted", "files",
                            time.strftime("%Y-%m-%d"), *parts)

    def first_backup(self, files, **settings):
        self.profile(cleanup="yes", **settings)
        self.set_proton(dict(self.STAY, **files))
        rv, _ = self.run_main()
        self.assertEqual(rv, 0)

    def test_old_copy_of_a_moved_file_is_deleted(self):
        self.first_backup({"Medical/2023/report.pdf": "blood test"})
        self.write(self.path("Medical", "2023", ".DS_Store"), "finder")
        self.set_proton(dict(self.STAY, **{"Health/2023/report.pdf": "blood test"}))
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(self.backup("Health/2023/report.pdf"), "blood test")
        self.assertFalse(os.path.exists(self.path("Medical")))
        self.assertIn("Moved on Proton, old copy deleted: 1", out)
        self.assertIn("Medical/2023/report.pdf\n       -> Health/2023/report.pdf", out)
        self.assertFalse(os.path.exists(os.path.dirname(self.held())))

    def test_renamed_file_keeps_only_the_new_name(self):
        self.first_backup({"a.txt": "alpha"})
        self.set_proton(dict(self.STAY, **{"b.txt": "alpha"}))
        self.run_main()
        self.assertEqual(self.backup("b.txt"), "alpha")
        self.assertFalse(os.path.exists(self.path("a.txt")))

    def test_file_deleted_on_proton_is_moved_aside(self):
        self.first_backup({"Old/Sub/gone.txt": "deleted on Proton"})
        self.set_proton(dict(self.STAY))
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(read(self.held("Old", "Sub", "gone.txt")), "deleted on Proton")
        self.assertFalse(os.path.exists(self.path("Old")))
        self.assertIn(f"Deleted on Proton, moved to {self.held()}: 1", out)
        self.assertIn("kept aside: 1 files", out)

    def test_files_set_aside_are_not_reported_again(self):
        self.first_backup({"gone.txt": "deleted on Proton"})
        self.set_proton(dict(self.STAY))
        self.run_main()
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertIn("Only in backup (not on Proton) 0", out)
        self.assertIn("kept aside: 1 files", out)

    def test_deleted_dir_from_the_profile(self):
        elsewhere = os.path.join(self.tmp, "Elsewhere")
        self.first_backup({"gone.txt": "deleted on Proton"}, deleted_dir=elsewhere)
        self.set_proton(dict(self.STAY))
        self.run_main()
        day = time.strftime("%Y-%m-%d")
        self.assertEqual(read(os.path.join(elsewhere, day, "gone.txt")), "deleted on Proton")
        self.assertFalse(os.path.exists(os.path.dirname(self.held())))

    def test_deleted_dir_inside_the_backup_is_left_out(self):
        inside = self.path("Set aside")
        self.first_backup({"gone.txt": "deleted on Proton"}, deleted_dir=inside)
        self.set_proton(dict(self.STAY))
        self.run_main()
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertIn("Only in backup (not on Proton) 0", out)
        day = time.strftime("%Y-%m-%d")
        self.assertEqual(read(os.path.join(inside, day, "gone.txt")), "deleted on Proton")

    def test_backup_at_the_root_of_a_disk_needs_a_deleted_dir(self):
        self.profile()
        self.set_proton(dict(self.STAY))
        dest = self.dest
        with mock.patch.object(os.path, "ismount", lambda p: p == dest):
            self.assertExits(lambda: self.run_main(), "is the root of a disk",
                             "Set deleted_dir")
            self.assertEqual(self.cli_calls("filesystem"), [])
            self.profile(deleted_dir=os.path.join(dest, "deleted", "files"))
            self.assertEqual(self.run_main()[0], 0)
            self.profile(cleanup="no")
            self.assertEqual(self.run_main()[0], 0)

    def test_same_file_deleted_twice_in_a_day_keeps_both(self):
        self.first_backup({"gone.txt": "first"})
        self.set_proton(dict(self.STAY))
        self.run_main()
        self.set_proton(dict(self.STAY, **{"gone.txt": "second"}))
        self.run_main()
        self.set_proton(dict(self.STAY))
        self.run_main()
        self.assertEqual(read(self.held("gone.txt")), "first")
        self.assertEqual(read(self.held("gone (2).txt")), "second")

    def test_moved_file_whose_new_copy_does_not_match_is_moved_aside(self):
        # verify = no: the new copy has the right size but other content, so
        # the old one is the only good copy and must not be deleted
        self.first_backup({"a.txt": "alpha"}, verify="no")
        self.write(self.path("b.txt"), "alphX")
        self.set_proton(dict(self.STAY, **{"b.txt": "alpha"}))
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(read(self.held("a.txt")), "alpha")
        self.assertNotIn("old copy deleted", out)

    def test_cleanup_is_on_by_default(self):
        self.profile()
        self.set_proton(dict(self.STAY, **{"gone.txt": "deleted on Proton"}))
        self.run_main()
        self.set_proton(dict(self.STAY))
        rv, _ = self.run_main()
        self.assertEqual(rv, 0)
        self.assertFalse(os.path.exists(self.path("gone.txt")))
        self.assertEqual(read(self.held("gone.txt")), "deleted on Proton")

    def test_cleanup_no_keeps_and_reports(self):
        self.first_backup({"gone.txt": "deleted on Proton"})
        self.profile(cleanup="no")
        self.set_proton(dict(self.STAY))
        _, out = self.run_main()
        self.assertEqual(self.backup("gone.txt"), "deleted on Proton")
        self.assertIn("NOT touched, cleanup = no", out)

    def test_command_line_overrides_the_profile(self):
        self.first_backup({"gone.txt": "deleted on Proton"})
        self.set_proton(dict(self.STAY))
        self.run_main("--no-cleanup")
        self.assertEqual(self.backup("gone.txt"), "deleted on Proton")
        self.profile(cleanup="no")
        self.run_main("--cleanup")
        self.assertFalse(os.path.exists(self.path("gone.txt")))
        self.assertTrue(os.path.exists(self.held("gone.txt")))

    def test_dry_run_shows_the_plan_and_touches_nothing(self):
        self.first_backup({"a.txt": "alpha", "gone.txt": "deleted on Proton"})
        self.set_proton(dict(self.STAY, **{"New/a.txt": "alpha"}))
        calls = len(self.downloads())
        rv, out = self.run_main("--dry-run")
        self.assertEqual(rv, 0)
        self.assertIn("a.txt\n       -> New/a.txt", out)
        self.assertIn("Deleted on Proton, moved to", out)
        self.assertIn("(--dry-run: nothing cleaned up)", out)
        self.assertEqual(len(self.downloads()), calls)
        self.assertEqual(self.backup("a.txt"), "alpha")
        self.assertEqual(self.backup("gone.txt"), "deleted on Proton")
        self.assertFalse(os.path.exists(os.path.dirname(self.held())))

    def test_failed_download_skips_the_cleanup(self):
        self.first_backup({"a.txt": "alpha"})
        self.set_proton(dict(self.STAY, **{"New/a.txt": "alpha"}), fail=["New/a.txt"])
        rv, out = self.run_main()
        self.assertEqual(rv, 1)
        self.assertIn("Cleanup skipped", out)
        self.assertEqual(self.backup("a.txt"), "alpha")
        self.set_proton(fail=[])
        rv, _ = self.run_main()
        self.assertEqual(rv, 0)
        self.assertFalse(os.path.exists(self.path("a.txt")))
        self.assertEqual(self.backup("New/a.txt"), "alpha")

    def test_too_much_to_clean_up_stops_before_downloading(self):
        self.first_backup({f"Old/f{i}.txt": f"old {i}" for i in range(12)})
        self.set_proton(dict(self.STAY, **{"new.txt": "new"}))
        calls = len(self.downloads())
        self.assertExits(lambda: self.run_main(), "cleanup would take 12 of the 22 files",
                         "--force-cleanup")
        self.assertEqual(len(self.downloads()), calls)
        self.assertEqual(len(os.listdir(self.path("Old"))), 12)
        rv, _ = self.run_main("--force-cleanup")
        self.assertEqual(rv, 0)
        self.assertFalse(os.path.exists(self.path("Old")))
        self.assertEqual(len(os.listdir(self.held("Old"))), 12)
        self.assertEqual(self.backup("new.txt"), "new")

    def test_a_few_removals_in_a_small_backup_are_allowed(self):
        self.profile(cleanup="yes")
        self.set_proton({"a.txt": "alpha", "b.txt": "beta"})
        self.run_main()
        self.set_proton({"a.txt": "alpha"})
        rv, _ = self.run_main()
        self.assertEqual(rv, 0)
        self.assertTrue(os.path.exists(self.held("b.txt")))


if __name__ == "__main__":
    unittest.main()
