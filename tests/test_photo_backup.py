# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
Unit tests for photo-backup.py: its settings (profiles, overrides), the
--setup questions, and end to end backups against a fake proton-drive CLI
that answers the Photos commands (timeline, download, albums) from a JSON
state. The configuration file itself is tested in test_protonlib.py.

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
from helpers import ScriptTestCase, read, sha1  # noqa: E402

FAKE_PHOTO_CLI = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Fake proton-drive for the Photos commands. $FAKE_STATE is a JSON file:
    {"photos": [{"uid": "u1", "name": "a.jpg", "content": "...",
                 "capture": "2023-05-01T10:00:00Z"}],
     "albums": [{"uid": "al1", "name": "Trip", "photos": ["u1"]}],
     "fail_download": ["u2"]}
    Every call is appended as a JSON list to $FAKE_STATE.calls."""
    import hashlib, json, os, sys
    st = json.load(open(os.environ["FAKE_STATE"], encoding="utf-8"))
    args = sys.argv[1:]
    with open(os.environ["FAKE_STATE"] + ".calls", "a", encoding="utf-8") as f:
        f.write(json.dumps(args, ensure_ascii=False) + "\\n")
    photos = {p["uid"]: p for p in st.get("photos", [])}

    if args == ["photo", "timeline", "-d", "-j"]:
        out = []
        for p in st.get("photos", []):
            b = p["content"].encode()
            out.append({"nodeUid": p["uid"], "name": {"ok": True, "value": p["name"]},
                        "photo": {"captureTime": p["capture"]},
                        "activeRevision": {"claimedSize": len(b),
                                           "claimedDigests": {"sha1": hashlib.sha1(b).hexdigest()}}})
        print(json.dumps(out)); sys.exit(0)
    if args[:4] == ["photo", "download", "-c", "rename"]:
        paths, target = args[4:-1], args[-1]
        failed = False
        for path in paths:
            uid = path.rsplit("/", 1)[1]
            if uid in st.get("fail_download", []):
                failed = True
                continue
            p = photos[uid]
            stem, ext = os.path.splitext(p["name"])
            dst, n = os.path.join(target, p["name"]), 1
            while os.path.exists(dst):
                dst = os.path.join(target, f"{stem} ({n}){ext}")
                n += 1
            with open(dst, "w", encoding="utf-8") as f:
                f.write(p["content"])
        if failed:
            print("download failed: network error", file=sys.stderr); sys.exit(1)
        sys.exit(0)
    if args == ["album", "list", "-j"]:
        print(json.dumps([{"uid": a["uid"], "name": {"ok": True, "value": a["name"]},
                           "album": {"photoCount": len(a["photos"])}}
                          for a in st.get("albums", [])]))
        sys.exit(0)
    if args[:3] == ["album", "photos", "-j"]:
        uid = args[3].rsplit("/", 1)[1]
        album = next(a for a in st.get("albums", []) if a["uid"] == uid)
        print(json.dumps([{"nodeUid": u} for u in album["photos"]])); sys.exit(0)
    print("unknown: " + " ".join(args), file=sys.stderr); sys.exit(2)
''')


class PhotoBackupTestCase(ScriptTestCase):
    """Loads protonlib and photo-backup.py fresh, with HOME in a temp folder."""

    SCRIPT = ("photo_backup", "photo-backup.py")

    def setUp(self):
        super().setUp()
        self.pb = self.mod

    def args(self, *argv):
        with mock.patch.object(sys, "argv", ["photo-backup.py", *argv]):
            return self.pb.parse_args()

    def resolve(self, *argv):
        return self.pb.resolve_settings(self.args(*argv, "--config", self.config))


# ---------------------------------------------------------------------------
# 1. Resolving the settings of a run
# ---------------------------------------------------------------------------

class ResolveSettingsTest(PhotoBackupTestCase):

    def setUp(self):
        super().setUp()
        self.dest = self.mkdir("Disk", "proton-photos")

    def test_profile_loads_its_section(self):
        albums = self.mkdir("Disk", "proton-albums")
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            albums = yes
            album_dir = {albums}
            batch = 3
            timeout = 60
            """)
        a = self.resolve("ssd")
        self.assertEqual(a.profile, "ssd")
        self.assertEqual(a.dest, self.dest)
        self.assertIs(a.albums, True)
        self.assertEqual(a.album_base, albums)
        self.assertEqual((a.batch, a.timeout), (3, 60))
        self.assertEqual(a.where, f"profile [photo-backup ssd] in {self.config}")

    def test_unset_keys_take_the_defaults(self):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            """)
        a = self.resolve("ssd")
        self.assertIs(a.albums, False)
        self.assertEqual(a.album_dir, "album")
        self.assertEqual(a.album_base, os.path.join(self.dest, "album"))
        self.assertEqual((a.batch, a.timeout), (5, 300))

    def test_single_profile_is_used_without_naming_it(self):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            """)
        a = self.resolve()
        self.assertEqual(a.profile, "ssd")
        self.assertEqual(a.dest, self.dest)

    def test_several_profiles_without_a_target_is_an_error(self):
        other = self.mkdir("Other")
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}

            [photo-backup other]
            dest = {other}
            """)
        self.assertExits(lambda: self.resolve(), "several photo-backup profiles", "ssd", "other")

    def test_no_profile_and_no_target_suggests_setup(self):
        self.assertExits(lambda: self.resolve(), "no photo-backup profile",
                         "./photo-backup.py --setup NAME")

    def test_folder_path_uses_the_defaults(self):
        a = self.resolve(self.dest)
        self.assertIsNone(a.profile)
        self.assertEqual(a.dest, self.dest)
        self.assertIs(a.albums, False)
        self.assertEqual(a.album_base, os.path.join(self.dest, "album"))
        self.assertEqual(a.where, "defaults and command line")

    def test_folder_path_takes_the_default_section(self):
        self.write_config("""
            [DEFAULT]
            batch = 2
            albums = yes
            """)
        a = self.resolve(self.dest)
        self.assertEqual(a.batch, 2)
        self.assertIs(a.albums, True)

    def test_profile_inherits_from_default_section(self):
        self.write_config(f"""
            [DEFAULT]
            timeout = 45
            albums = yes

            [photo-backup ssd]
            dest = {self.dest}
            albums = no
            """)
        a = self.resolve("ssd")
        self.assertEqual(a.timeout, 45)
        self.assertIs(a.albums, False)       # the profile wins over [DEFAULT]

    def test_folder_path_that_does_not_exist_is_accepted(self):
        # main() creates it: only a profile's dest has to exist already
        new = os.path.join(self.tmp, "New", "photos")
        a = self.resolve(new)
        self.assertEqual(a.dest, new)
        self.assertFalse(os.path.exists(new))

    def test_command_line_overrides_the_profile(self):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            albums = no
            album_dir = album
            batch = 5
            timeout = 300
            """)
        a = self.resolve("ssd", "--albums", "--album-dir", "views", "--batch", "9",
                         "--timeout", "30")
        self.assertIs(a.albums, True)
        self.assertEqual(a.album_base, os.path.join(self.dest, "views"))
        self.assertEqual((a.batch, a.timeout), (9, 30))

    def test_no_albums_overrides_albums_yes(self):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            albums = yes
            """)
        self.assertIs(self.resolve("ssd", "--no-albums").albums, False)

    def test_relative_album_dir_is_under_dest(self):
        a = self.resolve(self.dest, "--album-dir", "views/all")
        self.assertEqual(a.album_base, os.path.join(self.dest, "views", "all"))

    def test_absolute_album_dir_wins_over_dest(self):
        elsewhere = os.path.join(self.tmp, "Disk", "proton-albums")
        a = self.resolve(self.dest, "--album-dir", elsewhere)
        self.assertEqual(a.album_base, elsewhere)

    def test_tilde_album_dir_is_expanded(self):
        a = self.resolve(self.dest, "--album-dir", "~/albums")
        self.assertEqual(a.album_base, os.path.join(self.home, "albums"))

    def test_albums_and_no_albums_together_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                self.args(self.dest, "--albums", "--no-albums")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("not allowed with", err.getvalue())

    def test_invalid_albums_value_names_the_profile(self):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            albums = maybe
            """)
        self.assertExits(lambda: self.resolve("ssd"), "[photo-backup ssd]",
                         "albums = 'maybe'", "yes, no")

    def test_invalid_batch_and_timeout_are_errors(self):
        for key, value in (("batch", "0"), ("batch", "five"), ("timeout", "0"),
                           ("timeout", "-3")):
            with self.subTest(key=key, value=value):
                self.write_config(f"""
                    [photo-backup ssd]
                    dest = {self.dest}
                    {key} = {value}
                    """)
                self.assertExits(lambda: self.resolve("ssd"), f"{key} must be a number, 1 or more")

    def test_invalid_batch_on_the_command_line_is_an_error(self):
        self.assertExits(lambda: self.resolve(self.dest, "--batch", "0"),
                         "batch must be a number, 1 or more")

    def test_profile_dest_that_does_not_exist_stops_and_creates_nothing(self):
        unplugged = os.path.join(self.tmp, "Unplugged", "proton-photos")
        self.write_config(f"""
            [photo-backup ssd]
            dest = {unplugged}
            """)
        self.assertExits(lambda: self.resolve("ssd"), unplugged, "Is the disk plugged in?")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "Unplugged")))

    def test_profile_without_dest_suggests_setup(self):
        self.write_config("""
            [photo-backup ssd]
            albums = yes
            """)
        self.assertExits(lambda: self.resolve("ssd"), "has no dest",
                         "./photo-backup.py --setup ssd")

    def test_old_format_section_says_how_to_rename_it(self):
        self.write_config(f"""
            [ssd]
            dest = {self.dest}
            """)
        self.assertExits(lambda: self.resolve("ssd"), "old format", "[photo-backup ssd]")

    def test_unknown_profile_suggests_setup(self):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            """)
        self.assertExits(lambda: self.resolve("nas"), "'nas' is neither",
                         "profiles: ssd", "./photo-backup.py --setup nas")

    def test_other_scripts_profiles_are_not_photo_backup_profiles(self):
        self.write_config(f"""
            [file-backup ssd]
            dest = {self.dest}

            [drive-compare ssd]
            folder = {self.dest}
            """)
        self.assertExits(lambda: self.resolve(), "no photo-backup profile")
        self.assertExits(lambda: self.resolve("ssd"), "'ssd' is neither")

    def test_photo_backup_profile_ignores_other_scripts_sections(self):
        files = self.mkdir("Disk", "proton-files")
        self.write_config(f"""
            [file-backup ssd]
            dest = {files}
            batch = 20
            timeout = 1800

            [photo-backup ssd]
            dest = {self.dest}
            """)
        a = self.resolve()
        self.assertEqual(a.dest, self.dest)
        self.assertEqual((a.batch, a.timeout), (5, 300))

    def test_photo_backup_profile_is_not_a_file_backup_profile(self):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            """)
        fb = self.plib  # the choice of profile is shared code: check it from file-backup's side
        cfg = fb.read_config(self.config)
        self.assertEqual(fb.profile_names(cfg, "file-backup"), [])
        self.assertEqual(fb.profile_names(cfg, "drive-compare"), [])
        self.assertEqual(fb.profile_names(cfg, "photo-backup"), ["ssd"])

    def test_per_run_options_stay_on_the_command_line(self):
        self.write_config(f"""
            [photo-backup ssd]
            dest = {self.dest}
            limit = 3
            dry_run = yes
            """)
        a = self.resolve("ssd")
        self.assertEqual(a.limit, 0)
        self.assertIs(a.dry_run, False)
        a = self.resolve("ssd", "--limit", "2", "--dry-run", "--import", "~/Downloads",
                         "--reuse-inventory", "--skip-failed", "--redownload-missing")
        self.assertEqual(a.limit, 2)
        self.assertTrue(a.dry_run and a.reuse_inventory and a.skip_failed
                        and a.redownload_missing)
        self.assertEqual(a.import_dir, os.path.join(self.home, "Downloads"))


# ---------------------------------------------------------------------------
# 2. --setup: the photo-backup questions
# ---------------------------------------------------------------------------

class SetupTest(PhotoBackupTestCase):

    def setUp(self):
        super().setUp()
        self.dest = self.mkdir("Disk", "proton-photos")
        self.albums = os.path.join(self.tmp, "Disk", "proton-albums")

    def run_setup(self, answers, *argv, tty=True):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["photo-backup.py", "--setup", *argv,
                                             "--config", self.config]), \
                self.answering(answers, tty=tty) as prompts, contextlib.redirect_stdout(out):
            rv = self.pb.main()
        return rv, out.getvalue(), prompts

    def saved(self, name="ssd"):
        return dict(self.plib.read_config(self.config).items(f"photo-backup {name}"))

    def test_new_profile_with_explicit_answers(self):
        rv, out, _ = self.run_setup([self.dest, "yes", self.albums, "3", "60"], "ssd")
        self.assertEqual(rv, 0)
        self.assertEqual(self.saved(), {"dest": self.dest, "albums": "yes",
                                        "album_dir": self.albums, "batch": "3",
                                        "timeout": "60"})
        self.assertIn("Saved profile [photo-backup ssd]", out)
        self.assertIn("(or ./photo-backup.py ssd)", out)

    def test_enter_keeps_the_defaults_and_skips_album_dir(self):
        _, _, prompts = self.run_setup([self.dest, "", "", ""], "ssd")
        self.assertEqual(self.saved(), {"dest": self.dest, "albums": "no",
                                        "batch": "5", "timeout": "300"})
        self.assertFalse(any(p.strip().startswith("album_dir") for p in prompts))

    def test_album_dir_is_asked_only_when_albums_is_yes(self):
        _, _, prompts = self.run_setup([self.dest, "yes", "", "", ""], "ssd")
        self.assertTrue(prompts[2].strip().startswith("album_dir"))
        self.assertIn("(album)", prompts[2])
        self.assertEqual(self.saved()["album_dir"], "album")

    def test_new_profile_offers_an_example_dest(self):
        _, _, prompts = self.run_setup([self.dest, "", "", ""], "ssd")
        self.assertIn("(/Volumes/MyBackupDisk/proton-photos)", prompts[0])
        self.assertIn("albums [yes/no] (no)", prompts[1])

    def test_invalid_answers_are_asked_again(self):
        _, out, _ = self.run_setup([self.dest, "maybe", "no", "0", "x", "4", "-1", "90"], "ssd")
        self.assertEqual(self.saved(), {"dest": self.dest, "albums": "no",
                                        "batch": "4", "timeout": "90"})
        self.assertIn("answer one of: yes, no", out)
        self.assertEqual(out.count("a number, 1 or more"), 3)

    def test_missing_dest_can_be_kept_anyway(self):
        unplugged = "/Volumes/NotPluggedIn/proton-photos"
        _, out, _ = self.run_setup([unplugged, "yes", "", "", ""], "ssd")
        self.assertEqual(self.saved()["dest"], unplugged)
        self.assertIn("doesn't exist", out)

    def test_current_values_are_offered_as_defaults(self):
        self.write_config(f"""
            [DEFAULT]
            timeout = 45

            [photo-backup ssd]
            dest = {self.dest}
            albums = yes
            album_dir = {self.albums}
            """)
        _, _, prompts = self.run_setup([""] * 5, "ssd")
        self.assertIn(f"({self.dest})", prompts[0])
        self.assertIn("(yes)", prompts[1])
        self.assertIn(f"({self.albums})", prompts[2])
        self.assertIn("(45)", prompts[4])

    def test_saved_profile_resolves(self):
        self.run_setup([self.dest, "yes", self.albums, "2", ""], "ssd")
        a = self.resolve()
        self.assertEqual(a.dest, self.dest)
        self.assertIs(a.albums, True)
        self.assertEqual(a.album_base, self.albums)
        self.assertEqual((a.batch, a.timeout), (2, 300))

    def test_setup_keeps_other_scripts_profiles(self):
        self.write_config("""
            [file-backup ssd]
            dest = /data/files
            verify = no

            [drive-compare dropbox]
            folder = /data
            winner = ask
            """)
        self.run_setup([self.dest, "", "", ""], "ssd")
        cfg = self.plib.read_config(self.config)
        self.assertEqual(cfg.get("file-backup ssd", "verify"), "no")
        self.assertEqual(cfg.get("drive-compare dropbox", "winner"), "ask")
        self.assertIn("photo-backup ssd", cfg.sections())

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

PHOTOS = [
    {"uid": "u1", "name": "beach.jpg", "content": "beach pixels", "capture": "2021-07-14T09:00:00Z"},
    {"uid": "u2", "name": "snow.jpg", "content": "snow pixels", "capture": "2023-01-20T12:30:00Z"},
    {"uid": "u3", "name": "cake.jpg", "content": "cake pixels", "capture": "2023-06-02T18:00:00Z"},
]
ALBUMS = [{"uid": "al1", "name": "Holidays", "photos": ["u1", "u2"]}]


class EndToEndTest(PhotoBackupTestCase):

    def setUp(self):
        super().setUp()
        self.cli = self.write(os.path.join(self.tmp, "bin", "proton-drive"), FAKE_PHOTO_CLI)
        os.chmod(self.cli, 0o755)
        self.state = os.path.join(self.tmp, "state.json")
        env = mock.patch.dict(os.environ, {"FAKE_STATE": self.state})
        env.start()
        self.addCleanup(env.stop)
        self.set_state(PHOTOS, ALBUMS)
        self.dest = self.mkdir("Disk", "proton-photos")
        self.album_dir = os.path.join(self.tmp, "Disk", "proton-albums")

    def set_state(self, photos, albums=(), fail=()):
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"photos": list(photos), "albums": list(albums),
                       "fail_download": list(fail)}, f)

    def profile(self, **settings):
        lines = [f"dest = {self.dest}"] + [f"{k} = {v}" for k, v in settings.items()]
        self.write_config("[photo-backup ssd]\n" + "\n".join(lines) + "\n")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["photo-backup.py", *argv, "--config", self.config,
                                             "--cli", self.cli]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rv = self.pb.main()
        return rv, out.getvalue()

    def downloads(self):
        return self.cli_calls("photo", "download")

    def test_profile_run_downloads_into_year_folders(self):
        self.profile()
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(read(os.path.join(self.dest, "2021", "beach.jpg")), "beach pixels")
        self.assertEqual(read(os.path.join(self.dest, "2023", "snow.jpg")), "snow pixels")
        self.assertEqual(read(os.path.join(self.dest, "2023", "cake.jpg")), "cake pixels")
        self.assertIn(f"From   : profile [photo-backup ssd] in {self.config}", out)
        self.assertIn("Backup complete.", out)

    def test_second_run_downloads_nothing(self):
        self.profile()
        self.run_main()
        first = len(self.downloads())
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(len(self.downloads()), first)
        self.assertIn("to download: 0", out)

    def test_albums_yes_builds_album_folders_of_hard_links(self):
        self.profile(albums="yes", album_dir=self.album_dir)
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        album = os.path.join(self.album_dir, "Holidays")
        self.assertEqual(sorted(os.listdir(album)), ["beach.jpg", "snow.jpg"])
        for year, name in (("2021", "beach.jpg"), ("2023", "snow.jpg")):
            self.assertEqual(os.stat(os.path.join(album, name)).st_ino,
                             os.stat(os.path.join(self.dest, year, name)).st_ino)
        self.assertIn(f"Albums : {self.album_dir}", out)
        self.assertIn("Album links created / present .. 2 / 0", out)

    def test_default_album_dir_is_inside_dest_and_not_backed_up_twice(self):
        self.profile(albums="yes")
        self.run_main()
        self.assertTrue(os.path.isdir(os.path.join(self.dest, "album", "Holidays")))
        rv, out = self.run_main()
        self.assertEqual(rv, 0)
        self.assertNotIn("matching no Proton photo", out)
        self.assertIn("Album links created / present .. 0 / 2", out)

    def test_albums_no_reads_no_album_and_links_nothing(self):
        self.profile(albums="no", album_dir=self.album_dir)
        self.run_main()
        self.assertFalse(os.path.exists(self.album_dir))
        self.assertEqual(self.cli_calls("album"), [])

    def test_no_albums_overrides_a_profile_with_albums_yes(self):
        self.profile(albums="yes", album_dir=self.album_dir)
        rv, out = self.run_main("--no-albums")
        self.assertEqual(rv, 0)
        self.assertFalse(os.path.exists(self.album_dir))
        self.assertEqual(self.cli_calls("album"), [])
        self.assertNotIn("Albums :", out)

    def test_dry_run_downloads_nothing(self):
        self.profile(albums="yes", album_dir=self.album_dir)
        rv, out = self.run_main("--dry-run")
        self.assertEqual(rv, 0)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.cli_calls("album"), [])
        self.assertEqual(sorted(os.listdir(self.dest)), [])
        self.assertIn("to download: 3", out)
        self.assertIn("--dry-run: nothing downloaded", out)

    def test_profile_batch_size_is_respected(self):
        photos = [{"uid": f"p{i}", "name": f"img{i}.jpg", "content": f"pixels {i}",
                   "capture": "2022-03-01T10:00:00Z"} for i in range(5)]
        self.set_state(photos)
        self.profile(batch=2)
        self.run_main()
        calls = self.downloads()
        self.assertEqual([len(c) - 5 for c in calls], [2, 2, 1])   # minus command and folder
        self.assertEqual(len(os.listdir(os.path.join(self.dest, "2022"))), 5)

    def test_batch_on_the_command_line_overrides_the_profile(self):
        photos = [{"uid": f"p{i}", "name": f"img{i}.jpg", "content": f"pixels {i}",
                   "capture": "2022-03-01T10:00:00Z"} for i in range(4)]
        self.set_state(photos)
        self.profile(batch=1)
        self.run_main("--batch", "4")
        self.assertEqual(len(self.downloads()), 1)

    def test_missing_profile_dest_stops_before_any_cli_call(self):
        unplugged = os.path.join(self.tmp, "Unplugged", "proton-photos")
        self.write_config(f"[photo-backup ssd]\ndest = {unplugged}\n")
        self.assertExits(lambda: self.run_main(), "Is the disk plugged in?")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "Unplugged")))
        self.assertEqual(self.cli_calls(), [])

    def test_folder_path_is_created_and_backed_up(self):
        new = os.path.join(self.tmp, "New", "photos")
        rv, out = self.run_main(new)
        self.assertEqual(rv, 0)
        self.assertEqual(read(os.path.join(new, "2021", "beach.jpg")), "beach pixels")
        self.assertIn("From   : defaults and command line", out)

    def test_failed_photo_is_listed_and_retried_next_run(self):
        self.set_state(PHOTOS, ALBUMS, fail=["u2"])
        self.profile()
        rv, out = self.run_main()
        self.assertEqual(rv, 1)
        failed = read(os.path.join(self.dest, "photos-not-downloaded.txt"))
        self.assertEqual([l.split("\t")[0] for l in failed.splitlines() if l.strip()], ["u2"])
        self.assertFalse(os.path.exists(os.path.join(self.dest, "2023", "snow.jpg")))
        self.set_state(PHOTOS, ALBUMS)
        rv, _ = self.run_main()
        self.assertEqual(rv, 0)
        self.assertEqual(read(os.path.join(self.dest, "2023", "snow.jpg")), "snow pixels")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "photos-not-downloaded.txt")))
        self.assertEqual(self.downloads()[-1][4:-1], ["/photos/u2"])

    # the CLI's error text ends with a newline, and can span several lines
    def test_failed_photos_list_has_one_line_per_photo(self):
        self.set_state(PHOTOS, ALBUMS, fail=["u2"])
        self.profile()
        self.run_main()
        failed = read(os.path.join(self.dest, "photos-not-downloaded.txt"))
        self.assertEqual(len(failed.splitlines()), 1)

    def test_downloaded_files_match_the_proton_sha1(self):
        self.profile()
        self.run_main()
        photo_map = json.loads(read(os.path.join(self.dest, ".proton-photo-map.json")))
        self.assertEqual(sorted(photo_map), ["u1", "u2", "u3"])
        for p in PHOTOS:
            self.assertEqual(sha1(read(photo_map[p["uid"]])), sha1(p["content"]))


if __name__ == "__main__":
    unittest.main()
