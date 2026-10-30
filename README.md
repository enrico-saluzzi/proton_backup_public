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
- Everything that deletes or trashes is **dry-run by default**.

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
| back up my Drive files to a local disk | `file-backup.py` |
| check that a local folder (e.g. Dropbox) is all on Drive, and copy over what's missing | `drive-compare.py --copy-script` |

Both start by scanning Proton Drive through the CLI: path, size, date and
**sha1** of every file, in parallel, in seconds even on tens of thousands of
files (the macOS Drive folder takes hours to enumerate). `--root` picks the
remote folder (default `/my-files`, the whole Drive). The scan is saved in
`~/drive-sync-logs`: if it's interrupted or some folder can't be read, the
command stops, and running it again resumes the scan; `--reuse-scan` skips it
and uses the last complete one.

### Photos (Proton Photos)

| I want to... | Script |
|---|---|
| back up my Photos, in year folders and albums | `photo-backup.py --albums` |
| delete photos by browsing the backup, then trash them on Proton too | `photo-prune.py` |
| fix the date of scanned prints that sit in the wrong year | `photo-redate.py`, then `--apply` |
| check that a local photo folder (e.g. Camera Uploads) is all in Photos, and upload what's missing | `photo-compare.py --upload-script` |

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

```bash
./file-backup.py $B/proton-files --verify
```

- `--verify` recomputes the sha1 of every local file. Do it **every time**: it
  is the only check that catches a corrupted file of the exact same size.
  Expect roughly 20 minutes per 150 GB.

Note: Proton reports these hashes as `sha1Verified: false`, i.e. declared by
the uploading client, not recomputed by the servers. `--verify` confirms the
backup matches what Proton *declares* to hold.

#### Migrating from another cloud (e.g. Dropbox)

Before deleting the old copy, check that every file arrived on Drive (for the
camera folder, see *Photos* below).

```bash
./drive-compare.py ~/Dropbox --sha1 --exclude-dirs "Camera Uploads"
```

- If the content landed in a subfolder on Proton (check with
  `proton-drive filesystem list /my-files`), compare with that one:
  `--root /my-files/Dropbox`.
- `--exclude-dirs` takes top-level folder names, exact match.
- Make the Dropbox folders **available offline** first: online-only files have
  no local content and can only be listed as "not verified".
- Only the `TO-REVIEW-local-*` and `TO-REVIEW-proton-*` lists in
  `~/drive-sync-logs` need a decision; the rest is structural noise (macOS
  packages, Icon files, Office lock files).

To bring over what's missing, add `--copy-script`:

```bash
./drive-compare.py ~/Dropbox --sha1 --copy-script
```

It writes `copy-to-proton-<label>-<timestamp>.sh` next to the lists: it copies
the `TO-REVIEW-local` files into the folder synced by the Proton Drive app, at
the same relative path they have locally, creating subfolders as needed, and
the app uploads them in the background. Files already in the Proton folder are
never overwritten, and the script can be run again safely. It is only written,
not run: review it, run it, wait for the app to finish syncing, then compare
again.

The Proton Drive folder is found automatically in `~/Library/CloudStorage`
(with `--root /my-files/Work`, its `Work` subfolder). If there are several
(e.g. an old copy with a date in its name, left by a sync reset), the one
linked to the app is used. If it can't be found, pass it with
`--proton-folder`. As a safety check, the script isn't written if none of the
scanned top-level folders exist in the chosen folder.

### Photos (Proton Photos)

#### Monthly backup

```bash
./photo-backup.py $B/proton-photos --albums --album-dir $B/proton-albums
```

The result is two views of the same photos: `proton-photos/<year>/` in
chronological order, and `proton-albums/<album>/` organized like on Proton.

- Downloads go in batches of 5: bigger batches time out often on large
  libraries, and every timeout wastes the whole batch.
- The summary reports, with a full list next to the photos:
  - photos **deleted from the backup but still on Proton**: deleted on
    purpose, see *Pruning photos*; by accident, run again with
    `--redownload-missing`;
  - **local files that match no Proton photo** (`unrecognized-photos.txt`).
- Photos that can't be downloaded through the CLI (typically iOS edits,
  `_modified`) can be downloaded by hand from the web interface and added with
  `--import ~/Downloads`.

#### Pruning photos

Delete the photos you don't want **from the backup, in Finder**, looking at
them (the macOS Trash is your safety net). Then:

```bash
./photo-prune.py $B/proton-photos --from-missing --dry-run
./photo-prune.py $B/proton-photos --from-missing --apply
```

Then run the monthly photo backup again. Proton's trash has no expiry.

#### Fixing dates of scanned photos

Prints scanned years later have a capture date that is technically right but
semantically wrong: a 2003 event shows up in 2019. If your albums are named
starting with the year (`2003 - Holiday in Rome`), the albums know better:

```bash
./photo-redate.py $B/proton-photos --exclude-file excluded.txt   # plan only
./photo-redate.py $B/proton-photos --apply --limit 5             # try a few
./photo-redate.py $B/proton-photos --apply
```

Proton uses `DateTimeOriginal` for the timeline but has no command to edit
metadata, so the only way is to re-upload a corrected copy. `--apply` uses the
plan you reviewed, and is resumable.

- **Videos are skipped**: QuickTime date tags are ignored and a re-uploaded
  video lands at its upload date.
- **Side effect**: editing an album moves it to the top of the album list on
  Proton, and the order can't be changed.

#### Migrating from another cloud (e.g. Camera Uploads)

Typical case: a Dropbox `Camera Uploads` folder, while the same phone also
synced its photos to Proton Photos. Exclude that folder from `drive-compare.py`
and check it against the Photos section instead:

```bash
./photo-compare.py ~/Dropbox/"Camera Uploads" --upload-script
```

Names can't be compared (Dropbox renames uploads after their capture time,
the phone uploads `IMG_1234.HEIC`), and the bytes often differ too. So photos
are matched by **identical content** when the file is on disk, otherwise by
**capture time** (from the Dropbox file name, which works even on online-only
placeholders, or from EXIF), one Proton photo per local file, same kind
(photo/video). Times without a zone are read in this Mac's time zone (`--tz`
to change it).

- Each missing photo comes with a reason. "The Proton photo at that time
  matches another local file" means two local photos share the same second
  (a burst, or a duplicate made by Dropbox): worth a look.
- When in doubt a photo is reported missing: re-uploading costs at worst a
  duplicate, missing it costs the photo. Photos taken in another time zone
  may show up as missing for the same reason.
- The upload script uses `photo upload -c rename` and skips files that are
  empty when it runs: make the folder **available offline** before running it.
- The Proton timeline is saved in `~/drive-sync-logs`; pass it back with
  `--inventory` to avoid reading it again while you iterate.

---

## Files the scripts write

| File | Written by | Used by |
|---|---|---|
| `~/drive-sync-logs/<root>.tsv` (the Drive scan; `.state`, `.queue` while incomplete) | `file-backup.py`, `drive-compare.py` | the same, with `--reuse-scan` |
| `.proton-photo-inventory.json`, `.proton-photo-state.txt` | `photo-backup.py` | all photo scripts |
| `.proton-photo-map.json` (uid → local file) | `photo-backup.py` | `photo-prune.py`, `photo-redate.py` |
| `.proton-albums.json` | `photo-backup.py --albums` | `photo-prune.py`, `photo-redate.py` |
| `photos-deleted-locally.txt`, `unrecognized-photos.txt`, `photos-not-downloaded.txt` | `photo-backup.py` | you |
| `redate-plan.json`, `reupload-state.json` | `photo-redate.py` | `photo-redate.py --apply` |
| `prune-state.json` | `photo-prune.py` | the same, to resume |
| `~/drive-sync-logs/proton-photo-timeline-<timestamp>.json` | `photo-compare.py` | `photo-compare.py --inventory` |
| `~/drive-sync-logs/copy-to-proton-*.sh` | `drive-compare.py --copy-script` | you, after reviewing it |
| `~/drive-sync-logs/photos-upload-*.sh` | `photo-compare.py --upload-script` | you, after reviewing it |

Photo files live in the photo backup folder; Drive inventories, comparison
lists and upload scripts in `~/drive-sync-logs`.

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

**Enumerating Proton through the File Provider costs ~70 ms per file**, the CLI
~0.05 ms. On large folders that's the difference between seconds and hours.

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

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Enrico Saluzzi.

You can use, modify and redistribute these scripts freely, including in
commercial projects, as long as the copyright notice is kept. If they help
you, a mention is appreciated.
