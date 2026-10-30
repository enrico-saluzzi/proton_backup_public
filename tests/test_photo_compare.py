# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
Unit tests for photo-compare.py: its settings (profiles, overrides), the
--setup questions, and end to end comparisons against a fake proton-drive
that answers `photo timeline -d -j` from a JSON state. The configuration
file itself is tested in test_protonlib.py.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Every test runs with HOME pointing to a temporary folder, and the modules
are loaded fresh inside it, so nothing touches the real ~/drive-sync-logs,
~/.config/proton-backup or the Proton account.
"""

import contextlib
import glob
import io
import json
import os
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ScriptTestCase, read  # noqa: E402

FAKE_TIMELINE_CLI = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Fake proton-drive answering only `photo timeline -d -j`. $FAKE_STATE:
    {"photos": [{"uid": "u1", "name": "a.jpg", "content": "...",
                 "capture": "2024-05-03T10:22:11Z"}]}
    Every call is appended as a JSON list to $FAKE_STATE.calls."""
    import hashlib, json, os, sys
    st = json.load(open(os.environ["FAKE_STATE"], encoding="utf-8"))
    args = sys.argv[1:]
    with open(os.environ["FAKE_STATE"] + ".calls", "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\\n")
    if args == ["photo", "timeline", "-d", "-j"]:
        out = []
        for p in st.get("photos", []):
            b = p["content"].encode()
            out.append({"uid": p["uid"], "name": {"ok": True, "value": p["name"]},
                        "mediaType": "image/jpeg",
                        "photo": {"captureTime": p["capture"]},
                        "activeRevision": {"claimedSize": len(b),
                                           "claimedDigests": {"sha1": hashlib.sha1(b).hexdigest()}}})
        print(json.dumps(out)); sys.exit(0)
    print("unknown: " + " ".join(args), file=sys.stderr); sys.exit(2)
''')


class PhotoCompareTestCase(ScriptTestCase):
    """Loads protonlib and photo-compare.py fresh, with HOME in a temp folder."""

    SCRIPT = ("photo_compare", "photo-compare.py")

    def setUp(self):
        super().setUp()
        self.pc = self.mod
        self.camera = self.mkdir("Cloud", "Camera Uploads")

    def args(self, *argv):
        with mock.patch.object(sys, "argv", ["photo-compare.py", *argv]):
            return self.pc.parse_args()

    def resolve(self, *argv):
        return self.pc.resolve_settings(self.args(*argv, "--config", self.config))


# ---------------------------------------------------------------------------
# 1. Resolving the settings of a run
# ---------------------------------------------------------------------------

class ResolveSettingsTest(PhotoCompareTestCase):

    def test_profile_loads_its_section(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            tz = Europe/Rome
            tolerance = 3
            exif = no
            upload_script = yes
            batch = 7
            """)
        a = self.resolve("camera")
        self.assertEqual(a.profile, "camera")
        self.assertEqual(a.folders, [self.camera])
        self.assertEqual(a.tz, "Europe/Rome")
        self.assertEqual(str(a.tzinfo), "Europe/Rome")
        self.assertEqual((a.tolerance, a.batch), (3, 7))
        self.assertIs(a.no_exif, True)
        self.assertIs(a.upload_script, True)
        self.assertEqual(a.label, "camera")
        self.assertEqual(a.where, f"profile [photo-compare camera] in {self.config}")

    def test_unset_keys_take_the_defaults(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            """)
        a = self.resolve("camera")
        self.assertIsNone(a.tz)
        self.assertIsNone(a.tzinfo)
        self.assertEqual((a.tolerance, a.batch), (1, 20))
        self.assertIs(a.no_exif, False)
        self.assertIs(a.upload_script, False)

    def test_single_profile_is_used_without_naming_it(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            """)
        a = self.resolve()
        self.assertEqual(a.profile, "camera")
        self.assertEqual(a.folders, [self.camera])

    def test_several_profiles_without_a_target_is_an_error(self):
        other = self.mkdir("Phone")
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}

            [photo-compare phone]
            folder = {other}
            """)
        self.assertExits(lambda: self.resolve(), "several photo-compare profiles",
                         "camera", "phone")

    def test_no_profile_and_no_target_suggests_setup(self):
        self.assertExits(lambda: self.resolve(), "no photo-compare profile",
                         "./photo-compare.py --setup NAME")

    def test_other_scripts_profiles_are_not_photo_compare_profiles(self):
        dest = self.mkdir("Disk", "photos")
        self.write_config(f"""
            [photo-backup camera]
            dest = {dest}
            """)
        self.assertExits(lambda: self.resolve(), "no photo-compare profile")
        self.assertExits(lambda: self.resolve("camera"), "neither a photo-compare profile")

    def test_one_folder_path_uses_the_defaults(self):
        a = self.resolve(self.camera)
        self.assertIsNone(a.profile)
        self.assertEqual(a.folders, [self.camera])
        self.assertEqual(a.label, "Camera_Uploads")
        self.assertEqual(a.where, "defaults and command line")
        self.assertEqual(a.tolerance, 1)

    def test_several_folder_paths_are_all_compared(self):
        phone = self.mkdir("Phone")
        a = self.resolve(self.camera, phone)
        self.assertIsNone(a.profile)
        self.assertEqual(a.folders, [self.camera, phone])
        self.assertEqual(a.label, "Camera_Uploads")

    def test_with_several_targets_a_profile_name_is_a_folder(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            """)
        self.assertExits(lambda: self.resolve(self.camera, "camera"), "camera", "doesn't exist")

    def test_folder_path_takes_the_default_section(self):
        self.write_config("""
            [DEFAULT]
            tolerance = 0
            exif = no
            """)
        a = self.resolve(self.camera)
        self.assertEqual(a.tolerance, 0)
        self.assertIs(a.no_exif, True)

    def test_profile_inherits_from_default_section(self):
        self.write_config(f"""
            [DEFAULT]
            batch = 9
            tz = UTC

            [photo-compare camera]
            folder = {self.camera}
            tz = Europe/Rome
            """)
        a = self.resolve("camera")
        self.assertEqual(a.batch, 9)
        self.assertEqual(a.tz, "Europe/Rome")   # the profile wins over [DEFAULT]

    def test_command_line_overrides_the_profile(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            tz = Europe/Rome
            tolerance = 3
            exif = yes
            upload_script = no
            batch = 7
            """)
        a = self.resolve("camera", "--tz", "UTC", "--tolerance", "0", "--no-exif",
                         "--upload-script", "--batch", "2")
        self.assertEqual(a.tz, "UTC")
        self.assertEqual((a.tolerance, a.batch), (0, 2))
        self.assertIs(a.no_exif, True)
        self.assertIs(a.upload_script, True)

    def test_exif_overrides_exif_no_in_the_profile(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            exif = no
            """)
        self.assertIs(self.resolve("camera", "--exif").no_exif, False)

    def test_exif_and_no_exif_together_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.args(self.camera, "--exif", "--no-exif")
        self.assertEqual(cm.exception.code, 2)

    def test_setup_takes_a_single_profile_name(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.args("--setup", "one", "two")
        self.assertEqual(cm.exception.code, 2)

    def test_label_option_wins_and_is_sanitized(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            """)
        self.assertEqual(self.resolve("camera", "--label", "my phone/2024").label,
                         "my_phone_2024")

    def test_invalid_values_name_the_profile(self):
        for key, bad, needle in (("exif", "maybe", "exif = 'maybe'"),
                                 ("upload_script", "sure", "upload_script = 'sure'"),
                                 ("tolerance", "-1", "tolerance must be a number, 0 or more"),
                                 ("batch", "0", "batch must be a number, 1 or more")):
            with self.subTest(key=key):
                self.write_config(f"""
                    [photo-compare camera]
                    folder = {self.camera}
                    {key} = {bad}
                    """)
                self.assertExits(lambda: self.resolve("camera"), "[photo-compare camera]", needle)

    def test_unknown_time_zone_is_a_clear_error(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            tz = Mars/Base
            """)
        self.assertExits(lambda: self.resolve("camera"), "unknown time zone 'Mars/Base'",
                         "[photo-compare camera]")
        self.assertExits(lambda: self.resolve(self.camera, "--tz", "Not/AZone"),
                         "unknown time zone 'Not/AZone'")

    def test_profile_without_folder_is_an_error(self):
        self.write_config("""
            [photo-compare camera]
            tz = UTC
            """)
        self.assertExits(lambda: self.resolve("camera"), "has no folder",
                         "./photo-compare.py --setup camera")

    def test_profile_folder_that_does_not_exist_is_an_error(self):
        gone = os.path.join(self.tmp, "Gone")
        self.write_config(f"""
            [photo-compare camera]
            folder = {gone}
            """)
        self.assertExits(lambda: self.resolve("camera"), gone, "doesn't exist",
                         "[photo-compare camera]")

    def test_old_format_section_says_how_to_rename_it(self):
        self.write_config(f"""
            [camera]
            folder = {self.camera}
            """)
        self.assertExits(lambda: self.resolve("camera"), "old format",
                         "[photo-compare camera]")

    def test_unknown_profile_suggests_setup(self):
        self.assertExits(lambda: self.resolve("phone"), "neither a photo-compare profile",
                         "./photo-compare.py --setup phone")


# ---------------------------------------------------------------------------
# 2. The --setup questions
# ---------------------------------------------------------------------------

class SetupTest(PhotoCompareTestCase):

    def run_setup(self, answers, *argv, tty=True):
        with self.answering(answers, tty=tty) as prompts, \
                mock.patch.object(sys, "argv",
                                  ["photo-compare.py", "--setup", *argv,
                                   "--config", self.config]), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = self.pc.main()
        return rc, prompts

    def saved(self, name="camera"):
        cfg = self.plib.read_config(self.config)
        return dict(cfg.items(f"photo-compare {name}", raw=True))

    def test_new_profile_with_explicit_answers(self):
        rc, _ = self.run_setup([self.camera, "Europe/Rome", "2", "no", "yes", "10"], "camera")
        self.assertEqual(rc, 0)
        s = self.saved()
        self.assertEqual(s["folder"], self.camera)
        self.assertEqual(s["tz"], "Europe/Rome")
        self.assertEqual((s["tolerance"], s["exif"], s["upload_script"], s["batch"]),
                         ("2", "no", "yes", "10"))

    def test_enter_keeps_the_defaults_and_this_macs_time_zone(self):
        self.run_setup([self.camera, "", "", "", "", ""], "camera")
        s = self.saved()
        self.assertEqual(s["tz"], "")
        self.assertEqual((s["tolerance"], s["exif"], s["upload_script"], s["batch"]),
                         ("1", "yes", "no", "20"))

    def test_unknown_time_zone_is_asked_again(self):
        _, prompts = self.run_setup([self.camera, "Mars/Base", "UTC", "", "", "", ""], "camera")
        self.assertEqual(sum(1 for p in prompts if p.strip().startswith("tz")), 2)
        self.assertEqual(self.saved()["tz"], "UTC")

    def test_invalid_choices_and_numbers_are_asked_again(self):
        self.run_setup([self.camera, "", "-1", "1", "maybe", "no", "", "0", "5"], "camera")
        s = self.saved()
        self.assertEqual((s["tolerance"], s["exif"], s["batch"]), ("1", "no", "5"))

    def test_missing_folder_can_be_kept_anyway(self):
        gone = os.path.join(self.tmp, "Unplugged", "Camera")
        self.run_setup([gone, "yes", "", "", "", "", ""], "camera")
        self.assertEqual(self.saved()["folder"], gone)

    def test_saved_profile_resolves(self):
        self.run_setup([self.camera, "UTC", "0", "no", "no", "20"], "camera")
        a = self.resolve()
        self.assertEqual(a.profile, "camera")
        self.assertEqual((a.tz, a.tolerance, a.no_exif), ("UTC", 0, True))

    def test_other_scripts_profiles_are_kept(self):
        dest = self.mkdir("Disk", "photos")
        self.write_config(f"""
            [photo-backup ssd]
            dest = {dest}
            albums = yes
            """)
        self.run_setup([self.camera, "", "", "", "", ""], "camera")
        cfg = self.plib.read_config(self.config)
        self.assertEqual(cfg.get("photo-backup ssd", "albums"), "yes")
        self.assertTrue(cfg.has_section("photo-compare camera"))

    def test_refuses_without_a_terminal(self):
        with self.answering([], tty=False), \
                mock.patch.object(sys, "argv", ["photo-compare.py", "--setup", "camera",
                                                "--config", self.config]):
            self.assertExits(self.pc.main, "run it in a terminal")
        self.assertFalse(os.path.exists(self.config))


# ---------------------------------------------------------------------------
# 3. End to end, with a fake proton-drive
# ---------------------------------------------------------------------------

class EndToEndTest(PhotoCompareTestCase):

    def setUp(self):
        super().setUp()
        self.cli = self.write(os.path.join(self.tmp, "bin", "proton-drive"), FAKE_TIMELINE_CLI)
        os.chmod(self.cli, 0o755)
        self.state = os.path.join(self.tmp, "state.json")
        env = mock.patch.dict(os.environ, {"FAKE_STATE": self.state})
        env.start()
        self.addCleanup(env.stop)
        self.out = os.path.join(self.home, "drive-sync-logs")
        # on Proton: one photo by content, one by capture time (other bytes)
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"photos": [
                {"uid": "u1", "name": "IMG_0001.jpg", "content": "same bytes",
                 "capture": "2020-01-01T08:00:00Z"},
                {"uid": "u2", "name": "IMG_0002.HEIC", "content": "heic bytes",
                 "capture": "2024-05-03T10:22:11Z"},
            ]}, f)
        self.write(os.path.join(self.camera, "copy of one.jpg"), "same bytes")
        self.write(os.path.join(self.camera, "2024-05-03 10.22.11.jpg"), "jpg bytes")
        self.missing = self.write(os.path.join(self.camera, "2023-12-24 20.00.00.jpg"),
                                  "only here")

    def run_main(self, *argv):
        with mock.patch.object(sys, "argv", ["photo-compare.py", *argv, "--config", self.config,
                                             "--cli", self.cli]), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = self.pc.main()
        return rc, out.getvalue()

    def upload_script(self, label):
        found = glob.glob(os.path.join(self.out, f"photos-upload-{label}-*.sh"))
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_profile_run_writes_upload_script_with_the_profile_in_verify(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            tz = UTC
            exif = no
            upload_script = yes
            """)
        rc, out = self.run_main()
        self.assertEqual(rc, 1)                       # something is missing
        script = self.upload_script("camera")
        text = read(script)
        self.assertEqual(self.verify_line(script), "./photo-compare.py camera")
        self.assertIn(self.missing, text)
        self.assertNotIn("10.22.11", text)            # matched by capture time
        self.assertNotIn("copy of one", text)         # matched by content
        r = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        listed = glob.glob(os.path.join(self.out, "photos-missing-camera-*.txt"))
        self.assertEqual(len(listed), 1)
        self.assertIn(self.missing, read(listed[0]))
        self.assertEqual(self.timeline_calls(), [["photo", "timeline", "-d", "-j"]])

    def verify_line(self, script):
        """What the script prints as the command to verify the result."""
        lines = [l for l in read(script).splitlines()
                 if l.startswith("echo ") and "photo-compare.py" in l]
        self.assertEqual(len(lines), 1, lines)
        r = subprocess.run(["bash", "-c", lines[0]], capture_output=True, text=True)
        return r.stdout.strip()

    def timeline_calls(self):
        with open(self.state + ".calls", encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def camera_profile(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            tz = UTC
            exif = no
            """)

    def test_photo_libraries_are_skipped_and_named(self):
        library = os.path.join(self.camera, "Photos Library.photoslibrary")
        self.write(os.path.join(library, "resources", "derivatives", "thumb.jpg"), "thumb")
        self.write(os.path.join(library, "originals", "A", "IMG_9.heic"), "original")
        self.camera_profile()
        rc, out = self.run_main()
        self.assertIn("Skipped 1 photo libraries", out)
        self.assertIn(library, out)
        self.assertIn("Local photos and videos ........ 3", out)
        self.assertNotIn("thumb.jpg", out)

    def test_a_photo_library_as_the_folder_is_an_error(self):
        library = self.mkdir("Pictures", "Photos Library.photoslibrary")
        self.assertExits(lambda: self.run_main(library), "is a photo library",
                         "Export Unmodified Original")

    def test_timeline_is_saved_in_one_file_and_reused_with_reuse_scan(self):
        self.camera_profile()
        self.run_main()
        saved = os.path.join(self.out, "proton-photo-timeline.json")
        self.assertTrue(os.path.exists(saved))
        self.assertEqual(glob.glob(os.path.join(self.out, "proton-photo-timeline-*")), [])
        rc, out = self.run_main("--reuse-scan")
        self.assertEqual(rc, 1)
        self.assertIn("timeline reused", out)
        self.assertEqual(len(self.timeline_calls()), 1)     # read once, then reused

    def test_reuse_scan_without_a_saved_timeline_is_an_error(self):
        self.camera_profile()
        self.assertExits(lambda: self.run_main("--reuse-scan"), "no saved Proton timeline")
        self.assertFalse(os.path.exists(self.state + ".calls"))

    def test_recent_timeline_suggests_reuse_scan(self):
        self.camera_profile()
        self.run_main()
        rc, out = self.run_main()
        self.assertIn("--reuse-scan", out)
        self.assertIn("minute", out)
        self.assertEqual(len(self.timeline_calls()), 2)     # only a hint: read again

    def test_inventory_file_still_works(self):
        self.camera_profile()
        self.run_main()
        other = os.path.join(self.tmp, "other-timeline.json")
        os.replace(os.path.join(self.out, "proton-photo-timeline.json"), other)
        rc, out = self.run_main("--inventory", other)
        self.assertIn("timeline reused", out)
        self.assertEqual(len(self.timeline_calls()), 1)

    def test_folder_path_run_puts_the_folders_in_verify(self):
        rc, _ = self.run_main(self.camera, "--tz", "UTC", "--no-exif", "--upload-script")
        self.assertEqual(rc, 1)
        self.assertEqual(self.verify_line(self.upload_script("Camera_Uploads")),
                         f"./photo-compare.py '{self.camera}'")

    def test_wrong_time_zone_misses_the_capture_time_match(self):
        # the Dropbox name is local time: read in another zone it doesn't match
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            tz = Asia/Tokyo
            exif = no
            upload_script = yes
            """)
        self.run_main("camera")
        self.assertIn("10.22.11", read(self.upload_script("camera")))

    def test_no_upload_script_unless_asked(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            tz = UTC
            exif = no
            """)
        self.run_main()
        self.assertEqual(glob.glob(os.path.join(self.out, "photos-upload-*.sh")), [])

    def test_nothing_missing_exits_zero(self):
        os.remove(self.missing)
        rc, out = self.run_main(self.camera, "--tz", "UTC", "--no-exif")
        self.assertEqual(rc, 0)
        self.assertIn("Every local photo is on Proton", out)


if __name__ == "__main__":
    unittest.main()
