# proton_backup_public

Scripts to back up, verify and maintain a **Proton Drive** library (files *and*
Photos) from the command line, built around the official `proton-drive` CLI.

Written by **Enrico Saluzzi** during a migration from Dropbox to Proton Drive
(August 2026), after finding that the macOS desktop client couldn't enumerate
a large archive in reasonable time while the official CLI could. They have
been used since then for a monthly offline backup of a ~44,000-file Drive and
a ~36,000-photo library onto an external SSD.

- Python 3 standard library only: nothing to `pip install`.
- Every long-running script is **interruptible and resumable**.
- Everything that deletes or trashes is **off or dry-run by default**.

> Independent project, not affiliated with or endorsed by Proton AG.

---

## Getting started

1. Get the scripts, with git or as a ZIP (green **Code** button → **Download ZIP**
   on the repository page):

   ```bash
   git clone https://github.com/enrico-saluzzi/proton_backup_public.git
   cd proton_backup_public
   ```

   Keep all the files together: the scripts import `protonlib.py` from next to
   themselves.

2. Install the official Proton Drive CLI, `proton-drive`, and log in:

   ```bash
   proton-drive auth login
   ```

3. Run any script with `--help`: each one also has a detailed header
   explaining why it exists, what it does and what it can touch.

| Requirement | Needed by |
|---|---|
| Python 3.9 or later (`python3 --version`; on macOS it comes with the Command Line Tools) | all scripts |
| The Proton Drive CLI `proton-drive`, logged in | all scripts |
| `exiftool` (`brew install exiftool`) | `photo-redate.py --apply`; optional for `photo-compare.py` |

---

## At a glance

Six scripts, one per task: two for Drive files, four for Photos. Each runs as a
single command and does everything it needs by itself.

### Files (Proton Drive)

| I want to... | Script |
|---|---|
| back up my Drive files to a local disk | `file-backup.py` (set up once with `--setup`) |
| check that a folder (e.g. Dropbox, or a copy on a disk) is all on Drive, and copy over what's missing | `drive-compare.py` (set up once with `--setup`) |

Both start by scanning Proton Drive through the CLI: path, size, date and
**sha1** of every file. The CLI lists one folder per call, three calls in
parallel: count about a second per folder, so **half an hour or more** for a
Drive with a few thousand folders. `--root` picks the
remote folder (default `/my-files`, the whole Drive). The scan is saved in
`~/drive-sync-logs`: if it's interrupted or some folder can't be read, the
command stops, and running it again resumes the scan; `--reuse-scan` skips it
and uses the last complete one. A new scan replaces the last complete one
only when it completes, so stopping it never loses the previous scan. When the
last complete scan is less than an hour old, the script says so and waits 10
seconds before scanning again: Ctrl-C, then `--reuse-scan`, if nothing changed
on Proton since. The two scripts share the scan, so only one can scan a given
folder at a time: the second one stops and says to wait and then use
`--reuse-scan`.

Both read their choices from **profiles** in one configuration file, shared
with `photo-backup.py`: `~/.config/proton-backup/config.ini` (another one with
`--config`), a section per profile, `[file-backup ssd]`,
`[drive-compare dropbox]`, `[photo-backup ssd]`, with `[DEFAULT]` for what
they share. `--setup NAME` creates or changes a profile by asking
one question at a time; [`config.example.ini`](config.example.ini) documents
every setting. With a single profile, the script name alone is enough.
Options on the command line override the profile, and a folder path still
works instead of a profile name.

### Photos (Proton Photos)

| I want to... | Script |
|---|---|
| back up my Photos, in year folders and albums | `photo-backup.py` (set up once with `--setup`) |
| check that a local photo folder (e.g. Camera Uploads) is all in Photos, and upload what's missing | `photo-compare.py` (set up once with `--setup`) |
| delete photos by browsing the backup, then trash them on Proton too | `photo-prune.py` |
| fix the date of scanned prints that sit in the wrong year | `photo-redate.py`, then `--apply` |

The photo scripts use profiles too, in the same configuration file:
`[photo-backup ssd]` (the backup folder, the albums) and
`[photo-compare camera]` (the folder to check). `photo-prune.py` and
`photo-redate.py` work on the backup folder of the `photo-backup` profile
with the same name, so it's written once: with a single profile, no name is
needed at all. Their own sections, `[photo-redate ssd]` for instance with the
albums to leave alone, are optional.

Like the Drive scan, what the photo scripts read from Proton is saved and
reusable with the same option, `--reuse-scan`: the timeline and the album
list (`photo-backup.py`, in the backup folder) and the timeline
(`photo-compare.py`, in `~/drive-sync-logs`). The same hint appears when the
saved copy is less than an hour old, and the files are only replaced once
they're written completely.

The Photos section isn't synced by the desktop client and isn't a filesystem
path: it only exists through the CLI's `photo` and `album` commands, which is
what these scripts use. In the backup, photos are matched to local files by
**content** (sha1), not by name: the CLI renames duplicates, and identical
content can exist under several uids.

---

## Workflows

In the examples, `B` is the backup disk:

```bash
B=/Volumes/MyBackupDisk
```

### Files (Proton Drive)

#### Monthly backup

Once, describe the backup disk as a profile:

```bash
./file-backup.py --setup ssd
```

Then, every month:

```bash
./file-backup.py
```

- The backup verifies by content by default (`verify = yes`): it recomputes
  the sha1 of every local file, the only check that catches a corrupted file
  of the exact same size. Expect roughly 20 minutes per 150 GB. `--no-verify`
  skips it for one run.
- If the disk isn't plugged in, the script stops instead of creating the
  backup folder on the internal disk. (A folder path given on the command
  line, `./file-backup.py $B/proton-files`, is created if missing.)
- With `cleanup = yes` (off by default; `--cleanup`/`--no-cleanup` for one
  run) the backup follows Proton instead of keeping old files forever:
  - a file **moved or renamed** on Proton: its old copy is deleted, once the
    same content (sha1) is in the backup at the new path;
  - a file **deleted** on Proton: it's moved to `.deleted-on-proton/<date>/`
    inside the backup, with its path, in case the deletion was a mistake.
    The script never empties that folder and reminds you of its size at
    every run: delete what you no longer need.

  Folders left empty are removed. The cleanup runs only after every download
  succeeded, and stops if more than 10% of the backup would go (usually a
  wrong folder or `proton_root`): `--force-cleanup` if it's really what you
  want. With `cleanup = no` those files are only listed.
- `--dry-run` shows what would be downloaded and cleaned up.

Note: Proton reports these hashes as `sha1Verified: false`, i.e. declared by
the uploading client, not recomputed by the servers. Verification confirms
the backup matches what Proton *declares* to hold.

#### Migrating from another cloud (e.g. Dropbox)

Before deleting the old copy, check that nothing in it is missing on Drive
(for the camera folder, see *Photos* below). The same check works later on any
copy of your files, e.g. on a disk.

Describe the folder once, as a profile (`[drive-compare dropbox]` in the
configuration file):

```bash
./drive-compare.py --setup dropbox
```

Then compare whenever you want:

```bash
./drive-compare.py dropbox      # or just ./drive-compare.py with a single profile
```

The profile name is how the folder is called in the report ("Only in
dropbox"). A path works too (`./drive-compare.py ~/Dropbox`): the defaults
apply and the folder is called "folder". Options on the command line
(`--winner ask`, `--exclude "Camera Uploads"`, ...) override the profile.

What it does with each file in the folder:

| Situation | What happens |
|---|---|
| same path on Drive, same content (sha1) | identical, counted |
| same path, different content | decided by `winner`: `proton` keeps Drive's (default), `folder` copies the folder's over it, `newer` picks the most recently modified, `ask` lists it |
| not at that path, but found elsewhere on Drive | moved or renamed, counted (`moved = list` to see them) |
| listed in the ignore file | deleted from Drive on purpose, counted |
| system files, content of macOS packages | counted (Drive stores a package as one `.pkgf` file) |
| nowhere on Drive | **needs a decision**, with a note if something with that name is in the Proton trash |

Files only on Drive are counted, or listed with `proton_only = list`.

The result is **one report**, `~/drive-sync-logs/drive-compare-<profile>-<timestamp>.txt`:
what needs a decision on top, counts of everything else below. `--details`
appends the full lists to it.

- Moved files are found by sha1 when the file is on disk. An online-only
  placeholder has no content to hash, so it's matched **by name** (and size,
  when the placeholder reports it): the report says how many were matched by
  name only. Make the folder **available offline** for a check by content.
- The **ignore file** (`~/.config/proton-backup/ignore-<profile>.txt`) lists
  paths you deleted from Drive on purpose: one per line, a folder covers
  everything under it, `*` and `?` work as in the shell.
- The **Proton trash** is read only as a hint: the CLI can't list what's
  inside a trashed folder, and the trash is emptied after a while.
- If at most `review_max` files (default 20) need a decision and you're in a
  terminal, the script asks for each one: **c** copy to Drive, **i** ignore
  forever (added to the ignore file), **s** skip, **q** stop asking. With more
  files there's only the report: a review of hundreds of files never ends.
- If the content landed in a subfolder on Drive (check with
  `proton-drive filesystem list /my-files`), set `proton_root` (or `--root`)
  to it, e.g. `/my-files/Dropbox`.
- `exclude` takes top-level folder names, exact match, and applies to both
  sides.

When there's something to copy (files where the folder wins, files you chose
**c** for, and with `copy_script = yes` every file missing on Drive), the
script also writes `copy-to-proton-<profile>-<timestamp>.sh`: it copies those
files into the folder synced by the Proton Drive app, at the same relative
path, creating subfolders as needed, and the app uploads them in the
background. Missing files never overwrite anything; a file where the folder's
version wins replaces Drive's, which keeps the old one in its version
history. The script is only written, not run: review it, run it, wait for the
app to finish syncing, then compare again.

The Proton Drive folder is found automatically in `~/Library/CloudStorage`
(with `--root /my-files/Work`, its `Work` subfolder). If there are several
(e.g. an old copy with a date in its name, left by a sync reset), the one
linked to the app is used. If it can't be found, pass it with
`proton_folder` (or `--proton-folder`). As a safety check, the script isn't written if none of the
scanned top-level folders exist in the chosen folder.

### Photos (Proton Photos)

#### Monthly backup

Once, describe the backup as a profile: the photo folder, whether to build
the albums, and where:

```bash
./photo-backup.py --setup ssd
```

Then, every month:

```bash
./photo-backup.py
```

The result is two views of the same photos: `proton-photos/<year>/` in
chronological order, and `proton-albums/<album>/` organized like on Proton.

- Downloads go in batches of 5 (`batch`): bigger batches time out often on
  large libraries, and every timeout wastes the whole batch. If many calls
  time out, a shorter `timeout` than the default 300 s can make the run
  faster: a stuck call is given up sooner and retried on the next run.
- `album_dir` must be on the same disk as the photos: the albums are hard
  links. `--no-albums` skips them for one run.
- If the disk isn't plugged in, the script stops instead of creating the
  backup folder on the internal disk.
- The summary reports, with a full list next to the photos:
  - photos **deleted from the backup but still on Proton**: deleted on
    purpose, see *Pruning photos*; by accident, run again with
    `--redownload-missing`;
  - **local files that match no Proton photo** (`unrecognized-photos.txt`).
- Photos that can't be downloaded through the CLI (typically iOS edits,
  `_modified`) can be downloaded by hand from the web interface and added with
  `--import ~/Downloads`.

#### Migrating from another cloud (e.g. Camera Uploads)

Typical case: a Dropbox `Camera Uploads` folder, while the same phone also
synced its photos to Proton Photos. Exclude that folder from `drive-compare.py`
and check it against the Photos section instead:

```bash
./photo-compare.py --setup camera    # once: the folder, time zone, upload script
./photo-compare.py
```

(or, without a profile, `./photo-compare.py ~/Dropbox/"Camera Uploads" --upload-script`)

Names can't be compared (Dropbox renames uploads after their capture time,
the phone uploads `IMG_1234.HEIC`), and the bytes often differ too. So photos
are matched by **identical content** when the file is on disk, otherwise by
**capture time** (from the Dropbox file name, which works even on online-only
placeholders, or from EXIF), one Proton photo per local file, same kind
(photo/video). Times without a zone are read in this Mac's time zone (`tz` in
the profile, or `--tz`, to change it).

- Each missing photo comes with a reason. "The Proton photo at that time
  matches another local file" means two local photos share the same second
  (a burst, or a duplicate made by Dropbox): worth a look.
- When in doubt a photo is reported missing: re-uploading costs at worst a
  duplicate, missing it costs the photo. Photos taken in another time zone
  may show up as missing for the same reason.
- The upload script uses `photo upload -c rename` and skips files that are
  empty when it runs: make the folder **available offline** before running it.
- The Proton timeline is saved in `~/drive-sync-logs/proton-photo-timeline.json`;
  `--reuse-scan` reuses it instead of reading it again while you iterate,
  like the Drive scan.

#### Pruning photos

Delete the photos you don't want **from the backup, in Finder**, looking at
them (the macOS Trash is your safety net). Then:

```bash
./photo-prune.py --from-missing --dry-run
./photo-prune.py --from-missing --apply
```

Then run the monthly photo backup again. Proton's trash has no expiry.

#### Fixing dates of scanned photos

Prints scanned years later have a capture date that is technically right but
semantically wrong: a 2003 event shows up in 2019. If your albums are named
starting with the year (`2003 - Holiday in Rome`), the albums know better:

```bash
./photo-redate.py --setup ssd        # once: albums to leave alone (exclude_file)
./photo-redate.py                    # plan only
./photo-redate.py --apply --limit 5  # try a few
./photo-redate.py --apply
```

Proton uses `DateTimeOriginal` for the timeline but has no command to edit
metadata, so the only way is to re-upload a corrected copy. `--apply` uses the
plan you reviewed, and is resumable.

- **Videos are skipped**: QuickTime date tags are ignored and a re-uploaded
  video lands at its upload date.
- **Side effect**: editing an album moves it to the top of the album list on
  Proton, and the order can't be changed.

---

## Files the scripts write

| File | Written by | Used by |
|---|---|---|
| `~/drive-sync-logs/<root>.tsv` (the last complete Drive scan; `.partial`, `.state`, `.queue` while a scan is under way or interrupted) | `file-backup.py`, `drive-compare.py` | the same, with `--reuse-scan` |
| `.proton-photo-inventory.json`, `.proton-photo-state.txt` | `photo-backup.py` | all photo scripts |
| `.proton-photo-map.json` (uid → local file) | `photo-backup.py` | `photo-prune.py`, `photo-redate.py` |
| `.proton-albums.json` | `photo-backup.py --albums` | `photo-prune.py`, `photo-redate.py` |
| `photos-deleted-locally.txt`, `unrecognized-photos.txt`, `photos-not-downloaded.txt` | `photo-backup.py` | you |
| `redate-plan.json`, `reupload-state.json` | `photo-redate.py` | `photo-redate.py --apply` |
| `prune-state.json` | `photo-prune.py` | the same, to resume |
| `~/drive-sync-logs/proton-photo-timeline.json` | `photo-compare.py` | `photo-compare.py --reuse-scan` |
| `~/.config/proton-backup/config.ini` (the profiles) | `--setup` of either script, or you | `file-backup.py`, `drive-compare.py` |
| `~/.config/proton-backup/ignore-<profile>.txt` | `drive-compare.py` review (`i`), or you | `drive-compare.py` |
| `~/drive-sync-logs/drive-compare-<profile>-<timestamp>.txt` (the report) | `drive-compare.py` | you |
| `~/drive-sync-logs/copy-to-proton-*.sh` | `drive-compare.py`, when there's something to copy | you, after reviewing it |
| `~/drive-sync-logs/photos-upload-*.sh` | `photo-compare.py --upload-script` | you, after reviewing it |

Photo files live in the photo backup folder; Drive inventories, comparison
reports and upload scripts in `~/drive-sync-logs`; your choices in
`~/.config/proton-backup`.

---

## Lessons learned the hard way

**Online-only files report size 0.** Any tool that reads content (`git add`,
`tar`, `cp`, checksums) works on nothing without complaining. It doesn't fail:
it succeeds and produces a false result.

```bash
find FOLDER -type f -not -path '*/.git/*' -size 0 | wc -l
```

**The tool that does the work can't be the one that verifies it.** A check
that compares two sides produced by the same process will happily confirm the
same mistake on both.

**After every bulk operation, count the result with a different command.** It
catches errors that a "successful" output hides.

**CLI commands can exit successfully without doing anything.** `photo upload`
without `-c` asks for confirmation on stdin, uploads nothing, and returns 0.
Verify the effect, never the exit code.

**A sha1 doesn't identify a photo**: the same content can exist several times
under different uids. To recognize a new upload, diff the timeline before and
after.

**Normalize names for comparing, not for calling.** Proton keeps accents in
NFD on macOS: pass it the NFC form and it answers "Node not found".

**macOS doesn't allow `:` in file names** and the CLI replaces it with `_`.
Without accounting for that, the same file is both missing and orphaned.

**Enumerating Proton through the File Provider costs ~70 ms per file** and
gives no hashes. The CLI costs about a second per folder, returns the sha1 of
every file and works on a headless machine: slow too on a large Drive, so the
scan is saved, resumable, and reusable with `--reuse-scan`.

**The Photos section** isn't synced by the client and isn't a filesystem path:
it only exists through `photo timeline`, `photo download` and `album list`,
commands that are present in the CLI but not documented, and may change.

---

## Troubleshooting the macOS client

```bash
ps -Ao pcpu,comm | grep -i fileproviderd     # above 100% = problem
```

- **Logging out and back in from the app** fixes in seconds what a full resync
  fails to fix in hours.
- After logging out from the app, **run `proton-drive auth login` again**: the
  sessions are separate and the CLI otherwise hangs without saying so.
- Don't empty the trash from the web while a resync is running.
- `killall fileproviderd` is harmless: launchd restarts it.

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Standard library only. The tests run against temporary folders and a fake
`proton-drive`: they never touch your files or your Proton account.

---

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Enrico Saluzzi.

You can use, modify and redistribute these scripts freely, including in
commercial projects, as long as the copyright notice is kept. If they help
you, a mention is appreciated.
