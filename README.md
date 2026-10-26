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

## Requirements

| What | Needed by |
|---|---|
| Python 3 (`python3 --version`; on macOS it comes with the Command Line Tools) | all Python scripts |
| The official Proton Drive CLI `proton-drive`, logged in (`proton-drive auth login`) | almost all scripts |
| `exiftool` (`brew install exiftool`) | `photo-reupload.py` |
| `fswatch` (`brew install fswatch`) | `protondrive-lockfile-guard.sh` |

Keep all the files in the same folder: the Python scripts import
`protonlib.py` from next to themselves. Every script has a detailed header and
`--help`.

---

## The scripts

### Files (Proton Drive)

| Script | What it does |
|---|---|
| `proton-scan.py` | Inventory of a Drive tree via the CLI: path, size, date and **sha1** of every file, as a TSV. Parallel, resumable. The input of the next three. |
| `file-backup.py` | Incremental backup of Drive files to a local disk, with `--verify` to check every local file by content. |
| `drive-compare.py` | Compares a local folder (e.g. `~/Dropbox`) with the inventory, down to file content, without downloading anything. For verifying a migration. |
| `proton-orphans.py` | Folders that exist on Proton but no longer locally: candidates for cleanup. |

### Photos (Proton Photos)

The Photos section isn't synced by the desktop client and isn't a filesystem
path: it only exists through the CLI's `photo` and `album` commands.

| Script | What it does |
|---|---|
| `photo-backup.py` | Incremental backup of the Photos section into **one folder per capture year**, every download confirmed by sha1. |
| `photo-fixdate.py` | Restores capture dates on downloaded files, sorts them by year, and builds the **uid → local file map** the other photo scripts rely on. |
| `album-backup.py` | Saves the album structure and rebuilds every album as a folder of **hard links**: a second, album-based view of the same backup, using no extra space. |
| `photo-prune.py` | Propagates to Proton's trash the photos you deleted from the local backup in Finder. Refuses to empty an album. |
| `photo-redate.py` | Plans a date correction for scanned prints that sit in the wrong year on the timeline, using the year in the album name. Changes nothing. |
| `photo-reupload.py` | Applies that plan: fixes EXIF, re-uploads, re-adds to the album, trashes the original, restores the name. Six resumable phases. |
| `photo-import.py` | Adds to the backup photos you had to download by hand from the web interface. |
| `photo-unlinked.py` | Diagnostic: Proton photos with no matching local file, and why. |
| `photo-unrecognized.py` | Diagnostic: local files whose content matches no photo on Proton. |
| `photo-compare.py` | Which photos in one or more local folders (e.g. Dropbox Camera Uploads) are missing from Proton Photos, matched by content or capture time. Can write an upload script for the missing ones. |

### Sync client

| Script | What it does |
|---|---|
| `protondrive-lockfile-guard.sh` | Quarantines Office/LibreOffice lock files before the macOS File Provider tries to sync them; they were the suspected trigger of multi-hour sync hangs. |

---

## Workflows

In the examples, `B` is the backup disk:

```bash
B=/Volumes/MyBackupDisk
```

### Monthly file backup

```bash
./file-backup.py $B/proton-files --rescan --verify
```

- `--rescan` reads the inventory from Proton again before comparing. With a
  stale inventory, normal changes look like data loss.
- `--verify` recomputes the sha1 of every local file. Do it **every time**: it
  is the only check that catches a corrupted file of the exact same size.
  Expect roughly 20 minutes per 150 GB.

Note: Proton reports these hashes as `sha1Verified: false`, i.e. declared by
the uploading client, not recomputed by the servers. `--verify` confirms the
backup matches what Proton *declares* to hold.

### Monthly photo backup

```bash
./photo-backup.py $B/proton-photos --details --batch 5 --timeout 60
./photo-fixdate.py $B/proton-photos --reorganize
./album-backup.py $B/proton-photos --album-dir $B/proton-albums
```

- `--details` is essential: without it the inventory has no sha1 and
  downloads can only be confirmed by counting files, which is misleading.
- `--batch 5`: bigger batches time out often on large libraries, and every
  timeout wastes the whole batch.
- **Order matters**: `photo-fixdate.py` produces the uid → file map that
  `album-backup.py` (and pruning) depend on. Always run it in between.

The result is two views of the same photos: `proton-photos/<year>/` in
chronological order, and `proton-albums/<album>/` organized like on Proton.

If `album-backup.py` reports photos "not linked", `photo-unlinked.py` lists
exactly which ones and why.

### Pruning photos

Delete the photos you don't want **from the backup, in Finder**, looking at
them (the macOS Trash is your safety net). Then:

```bash
./photo-prune.py $B/proton-photos --from-missing --dry-run
./photo-prune.py $B/proton-photos --from-missing --apply
```

Then run the monthly photo backup again, **without** `--reuse` on
`album-backup.py`: a reused album map still holds uids now in the trash, which
would show up as "not linked". Proton's trash has no expiry.

### Fixing dates of scanned photos

Prints scanned years later have a capture date that is technically right but
semantically wrong: a 2003 event shows up in 2019. If your albums are named
starting with the year (`2003 - Holiday in Rome`), the albums know better:

```bash
./photo-redate.py $B/proton-photos --exclude-file excluded.txt    # plan only
./photo-reupload.py $B/proton-photos --apply --limit 5 --skip-video  # try a few
./photo-reupload.py $B/proton-photos --apply --skip-video
```

Proton uses `DateTimeOriginal` for the timeline but has no command to edit
metadata, so the only way is to re-upload a corrected copy.

- **Videos don't work**: QuickTime date tags are ignored and the video lands at
  its upload date. Always use `--skip-video`.
- **Side effect**: editing an album moves it to the top of the album list on
  Proton, and the order can't be changed.

### Verifying a migration (e.g. Dropbox → Proton)

```bash
./proton-scan.py /my-files --workers 3 --delay 0.2 --timeout 300
./drive-compare.py ~/Dropbox my-files.tsv --sha1 --label check-$(date +%Y%m) \
    --exclude-dirs "Camera Uploads"
```

- Find where the content landed on Proton first
  (`proton-drive filesystem list /my-files`) and scan that folder.
- `--exclude-dirs` takes top-level folder names, exact match.
- Make the Dropbox folders **available offline** first: online-only files have
  no local content and can only be listed as "not verified".
- Only the `TO-REVIEW-local-*` and `TO-REVIEW-proton-*` lists in
  `~/drive-sync-logs` need a decision; the rest is structural noise (macOS
  packages, Icon files, Office lock files).

To bring over what's missing, add `--copy-script`:

```bash
./drive-compare.py ~/Dropbox my-files.tsv --sha1 --copy-script
```

It writes `copy-to-proton-<label>-<timestamp>.sh` next to the lists: it copies
the `TO-REVIEW-local` files into the folder synced by the Proton Drive app, at
the same relative path they have locally, creating subfolders as needed, and
the app uploads them in the background. Files already in the Proton folder are
never overwritten, and the script can be run again safely. It is only written,
not run: review it, run it, wait for the app to finish syncing, then scan and
compare again.

The Proton Drive folder is found automatically in `~/Library/CloudStorage`. If
there are several (e.g. an old copy with a date in its name, left by a sync
reset), the one linked to the app is used. If you scanned a subfolder, or the
folder can't be found, pass it explicitly:

```bash
./drive-compare.py ~/Dropbox my-files-Work.tsv --sha1 --copy-script \
    --proton-folder ~/Library/CloudStorage/ProtonDrive-you@example.com-folder/Work
```

As a safety check, the script isn't written if none of the scanned top-level
folders exist in the chosen folder.

### Checking a photo folder against Proton Photos (e.g. Camera Uploads)

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
| `~/drive-sync-logs/<root>.tsv` (+ `.state`, `.queue`) | `proton-scan.py` | `file-backup.py`, `drive-compare.py`, `proton-orphans.py` |
| `.proton-photo-inventory.json` | `photo-backup.py` | all photo scripts |
| `.proton-photo-state.txt` | `photo-backup.py`, `photo-import.py` | `photo-backup.py` |
| `.proton-photo-map.json` | `photo-fixdate.py` | `album-backup.py`, `photo-prune.py`, `photo-redate.py`, `photo-unlinked.py` |
| `.proton-albums.json` | `album-backup.py` | `photo-prune.py`, `photo-redate.py`, `photo-reupload.py`, `photo-unlinked.py` |
| `redate-plan.json` | `photo-redate.py` | `photo-reupload.py` |
| `prune-state.json`, `reupload-state.json` | `photo-prune.py`, `photo-reupload.py` | the same, to resume |
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
