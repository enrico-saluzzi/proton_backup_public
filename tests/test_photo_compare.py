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
    """Fake proton-drive: `photo timeline -d -j`, `album list -j`, `album photos`,
    and `photo upload`, `album create`, `album add-photo`, which change the
    state. $FAKE_STATE:
    {"photos": [{"uid": "u1", "name": "a.jpg", "content": "...",
                 "capture": "2024-05-03T10:22:11Z", "albums": ["a1"],
                 "hidden_reads": 0}],
     "albums": [{"uid": "a1", "name": "Wedding"}], "album_list_fails": false,
     "upload_hidden_reads": 0, "add_fails": false}
    A photo with hidden_reads > 0 is left out of that many timeline reads (not
    indexed yet); uploads get upload_hidden_reads. Every call is appended as
    a JSON list to $FAKE_STATE.calls."""
    import hashlib, json, os, sys
    st = json.load(open(os.environ["FAKE_STATE"], encoding="utf-8"))
    args = sys.argv[1:]
    with open(os.environ["FAKE_STATE"] + ".calls", "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\\n")
    def save():
        json.dump(st, open(os.environ["FAKE_STATE"], "w", encoding="utf-8"))

    def album_of(path):
        key = path.split("/", 2)[-1]
        return next((x for x in st.get("albums", []) if key in (x["uid"], x["name"])), None)

    if args == ["photo", "timeline", "-d", "-j"]:
        out = []
        for p in st.get("photos", []):
            if p.get("hidden_reads", 0) > 0:
                p["hidden_reads"] -= 1
                continue
            b = p["content"].encode()
            out.append({"uid": p["uid"], "name": {"ok": True, "value": p["name"]},
                        "mediaType": "image/jpeg",
                        "photo": {"captureTime": p["capture"],
                                  "albums": [{"nodeUid": u} for u in p.get("albums", [])]},
                        "activeRevision": {"claimedSize": len(b),
                                           "claimedDigests": {"sha1": hashlib.sha1(b).hexdigest()}}})
        save()
        print(json.dumps(out)); sys.exit(0)
    if args == ["album", "list", "-j"]:
        if st.get("album_list_fails"):
            print("network error", file=sys.stderr); sys.exit(1)
        print(json.dumps([{"uid": x["uid"], "name": {"ok": True, "value": x["name"]},
                           "type": "album"} for x in st.get("albums", [])]))
        sys.exit(0)
    if args[:2] == ["album", "photos"]:
        sys.exit(0 if album_of(args[-1]) else 1)
    if args[:2] == ["album", "create"]:
        st.setdefault("albums", []).append({"uid": "a-new", "name": args[2]})
        save(); sys.exit(0)
    if args[:2] == ["album", "add-photo"]:
        alb = album_of(args[2])
        if alb is None or st.get("add_fails"):
            print("album add failed", file=sys.stderr); sys.exit(1)
        for path in args[3:]:
            for p in st["photos"]:
                if p["uid"] == path.split("/")[-1]:
                    p.setdefault("albums", []).append(alb["uid"])
        save(); sys.exit(0)
    if args[:4] == ["photo", "upload", "-c", "rename"]:
        for path in args[4:]:
            n = len(st["photos"]) + 1
            st["photos"].append({"uid": f"new{n}", "name": os.path.basename(path),
                                 "content": open(path, encoding="utf-8").read(),
                                 "capture": "2026-01-01T00:00:00Z",
                                 "hidden_reads": st.get("upload_hidden_reads", 0)})
        save(); sys.exit(0)
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
        rc, _ = self.run_setup([self.camera, "Europe/Rome", "2", "no", "yes", "10",
                                "2026 - Wedding"], "camera")
        self.assertEqual(rc, 0)
        s = self.saved()
        self.assertEqual(s["folder"], self.camera)
        self.assertEqual(s["tz"], "Europe/Rome")
        self.assertEqual((s["tolerance"], s["exif"], s["upload_script"], s["batch"]),
                         ("2", "no", "yes", "10"))
        self.assertEqual(s["album"], "2026 - Wedding")

    def test_album_is_asked_only_with_the_upload_script(self):
        _, prompts = self.run_setup([self.camera, "", "", "", "no", ""], "camera")
        self.assertFalse(any(p.strip().startswith("album") for p in prompts))
        self.assertEqual(self.saved()["album"], "")

    def test_album_none_means_no_album_and_slashes_are_refused(self):
        _, prompts = self.run_setup([self.camera, "", "", "", "yes", "", "a/b", "none"],
                                    "camera")
        self.assertEqual(sum(1 for p in prompts if p.strip().startswith("album")), 2)
        self.assertEqual(self.saved()["album"], "")

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

    # album

    def set_albums(self, albums, members=()):
        with open(self.state, encoding="utf-8") as f:
            st = json.load(f)
        st["albums"] = albums
        for p in st["photos"]:
            p["albums"] = [a for u, a in members if u == p["uid"]]
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def run_album(self, *extra):
        return self.run_main(self.camera, "--tz", "UTC", "--no-exif",
                             "--album", "Wedding", *extra)

    def calls_after(self, script):
        """Run the generated script against the fake CLI; the calls it made."""
        before = len(self.timeline_calls())
        r = subprocess.run(["bash", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return self.timeline_calls()[before:], r.stdout

    def test_new_album_is_created_and_gets_the_photos_already_on_proton(self):
        self.set_albums([{"uid": "a9", "name": "Other"}])
        rc, out = self.run_album()
        self.assertEqual(rc, 1)
        self.assertIn('Album "Wedding": not on Proton yet, the script creates it', out)
        self.assertIn("to add, already on Proton: 2", out)
        self.assertIn("to add after the upload: 1 (the script does it at the end)", out)
        script = self.upload_script("Camera_Uploads")
        calls, stdout = self.calls_after(script)
        self.assertEqual(calls[0], ["album", "photos", "/albums/Wedding"])
        self.assertEqual(calls[1], ["album", "create", "Wedding"])
        self.assertEqual(calls[2], ["photo", "upload", "-c", "rename", self.missing])
        self.assertEqual(calls[3], ["album", "add-photo", "/albums/Wedding",
                                    "/photos/u1", "/photos/u2"])
        # --finish-album: the uploaded photo, found by content
        self.assertIn(["album", "add-photo", "/albums/a-new", "/photos/new3"], calls[4:])
        self.assertIn("Added to the album: 2", stdout)
        self.assertIn("Added to the album: 1   failed: 0   not found on Proton: 0", stdout)
        # one run was enough
        rc, out = self.run_album()
        self.assertEqual(rc, 0)
        self.assertIn("Every local photo is on Proton, and in the album.", out)
        self.assertEqual(self.verify_line(script),
                         f"./photo-compare.py '{self.camera}' --album Wedding")

    def test_script_run_again_does_not_create_a_second_album(self):
        self.set_albums([])
        self.run_album()
        script = self.upload_script("Camera_Uploads")
        self.set_albums([{"uid": "a1", "name": "Wedding"}])     # created by the first run
        calls, _ = self.calls_after(script)
        self.assertNotIn(["album", "create", "Wedding"], calls)

    def test_album_names_are_never_expanded_by_the_shell(self):
        self.set_albums([])
        self.run_main(self.camera, "--tz", "UTC", "--no-exif", "--album", "Cost $HOME `x`")
        calls, _ = self.calls_after(self.upload_script("Camera_Uploads"))
        self.assertIn(["album", "create", "Cost $HOME `x`"], calls)

    def test_existing_album_gets_only_the_photos_not_in_it_by_uid(self):
        self.set_albums([{"uid": "a1", "name": "Wedding"}], members=[("u1", "a1")])
        _, out = self.run_album()
        self.assertIn('Album "Wedding": 1 of these photos already in it', out)
        calls, _ = self.calls_after(self.upload_script("Camera_Uploads"))
        self.assertNotIn("create", [c[1] for c in calls])
        self.assertIn(["album", "add-photo", "/albums/a1", "/photos/u2"], calls)

    def test_everything_on_proton_and_in_the_album_writes_no_script(self):
        os.remove(self.missing)
        self.set_albums([{"uid": "a1", "name": "Wedding"}],
                        members=[("u1", "a1"), ("u2", "a1")])
        rc, out = self.run_album()
        self.assertEqual(rc, 0)
        self.assertIn("nothing to upload or add to the album", out)
        self.assertIn("Every local photo is on Proton, and in the album.", out)
        self.assertEqual(glob.glob(os.path.join(self.out, "photos-upload-*.sh")), [])

    def test_photos_on_proton_but_not_in_the_album_exit_1(self):
        os.remove(self.missing)
        self.set_albums([{"uid": "a1", "name": "Wedding"}], members=[("u1", "a1")])
        rc, out = self.run_album()
        self.assertEqual(rc, 1)
        calls, _ = self.calls_after(self.upload_script("Camera_Uploads"))
        self.assertEqual(calls, [["album", "add-photo", "/albums/a1", "/photos/u2"]])

    def test_album_in_the_profile_writes_the_script_and_keeps_verify_short(self):
        self.write_config(f"""
            [photo-compare camera]
            folder = {self.camera}
            tz = UTC
            exif = no
            album = Wedding
            """)
        self.set_albums([])
        self.run_main()
        script = self.upload_script("camera")
        self.assertEqual(self.verify_line(script), "./photo-compare.py camera")
        self.assertIn('# Album "Wedding": created first', read(script))

    def test_two_albums_with_the_same_name_is_an_error(self):
        self.set_albums([{"uid": "a1", "name": "Wedding"}, {"uid": "a2", "name": "Wedding"}])
        self.assertExits(self.run_album, "2 albums on Proton are called 'Wedding'")

    def test_unreadable_album_list_is_an_error(self):
        with open(self.state, encoding="utf-8") as f:
            st = json.load(f)
        st["album_list_fails"] = True
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump(st, f)
        self.assertExits(self.run_album, "ERROR from 'album list'")

    def test_album_name_with_a_slash_is_an_error(self):
        self.assertExits(lambda: self.run_main(self.camera, "--album", "a/b"),
                         "can't contain '/'")

    # --finish-album, called by the script after the uploads

    def update_state(self, **kw):
        with open(self.state, encoding="utf-8") as f:
            st = json.load(f)
        st.update(kw)
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def compare_then_upload(self):
        """Compare with a new album, then upload as the script would; returns
        the manifest the script would hand to --finish-album."""
        self.set_albums([{"uid": "a1", "name": "Wedding"}])
        self.run_album()
        found = glob.glob(os.path.join(self.out, "photos-album-Camera_Uploads-*.json"))
        self.assertEqual(len(found), 1, found)
        subprocess.run([self.cli, "photo", "upload", "-c", "rename", self.missing], check=True)
        return found[0]

    def finish(self, manifest):
        self.pc.FINISH_WAIT = 0
        with mock.patch.object(sys, "argv", ["photo-compare.py", "--finish-album", manifest,
                                             "--cli", self.cli]), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = self.pc.main()
        return rc, out.getvalue()

    def test_manifest_lists_the_files_to_upload(self):
        manifest = self.compare_then_upload()
        m = json.loads(read(manifest))
        self.assertEqual((m["album"], m["paths"]), ("Wedding", [self.missing]))

    def test_finish_waits_for_proton_to_index_the_uploads(self):
        self.update_state(upload_hidden_reads=2)
        rc, out = self.finish(self.compare_then_upload())
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("not on the timeline yet"), 2)
        self.assertIn("Added to the album: 1   failed: 0   not found on Proton: 0", out)
        self.assertIn(["album", "add-photo", "/albums/a1", "/photos/new3"],
                      self.timeline_calls())

    def test_finish_gives_up_and_says_what_to_run_later(self):
        self.update_state(upload_hidden_reads=99)
        rc, out = self.finish(self.compare_then_upload())
        self.assertEqual(rc, 1)
        self.assertEqual(out.count("not on the timeline yet"), self.pc.FINISH_TRIES - 1)
        self.assertIn("not found on Proton: 1", out)
        self.assertIn(f"not found: {self.missing}", out)
        self.assertIn("Run this again later:", out)

    def test_finish_reports_a_failed_add(self):
        self.update_state(add_fails=True)
        rc, out = self.finish(self.compare_then_upload())
        self.assertEqual(rc, 1)
        self.assertIn("Added to the album: 0   failed: 1", out)

    def test_finish_skips_files_the_script_did_not_upload(self):
        manifest = self.compare_then_upload()
        open(self.missing, "w").close()          # empty now: the script skips it
        rc, out = self.finish(manifest)
        self.assertEqual(rc, 0)
        self.assertIn("adding the 0 photos just uploaded", out)

    def test_finish_leaves_alone_a_photo_already_in_the_album(self):
        manifest = self.compare_then_upload()
        with open(self.state, encoding="utf-8") as f:
            st = json.load(f)
        st["photos"][-1]["albums"] = ["a1"]
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump(st, f)
        rc, out = self.finish(manifest)
        self.assertEqual(rc, 0)
        self.assertFalse(any(c[:2] == ["album", "add-photo"] for c in self.timeline_calls()))

    def test_finish_without_the_album_is_an_error(self):
        manifest = self.compare_then_upload()
        self.set_albums([])
        self.pc.FINISH_WAIT = 0
        self.assertExits(lambda: self.finish(manifest), "no album called 'Wedding'")

    def test_finish_with_an_unreadable_manifest_is_an_error(self):
        bad = self.write(os.path.join(self.tmp, "bad.json"), "{not json")
        self.assertExits(lambda: self.finish(bad), "can't read")

    def test_script_skips_finish_when_nothing_was_uploaded(self):
        self.set_albums([{"uid": "a1", "name": "Wedding"}])
        self.run_album()
        script = self.upload_script("Camera_Uploads")
        open(self.missing, "w").close()
        calls, _ = self.calls_after(script)
        self.assertFalse(any(c[:3] == ["photo", "timeline", "-d"] for c in calls))

    def test_no_manifest_without_photos_to_upload(self):
        os.remove(self.missing)
        self.set_albums([{"uid": "a1", "name": "Wedding"}])
        self.run_album()
        self.assertEqual(glob.glob(os.path.join(self.out, "photos-album-*.json")), [])
        self.assertNotIn("--finish-album", read(self.upload_script("Camera_Uploads")))

    def test_no_album_list_call_without_an_album(self):
        self.run_main(self.camera, "--tz", "UTC", "--no-exif")
        self.assertFalse(any(c[:1] == ["album"] for c in self.timeline_calls()))


if __name__ == "__main__":
    unittest.main()
