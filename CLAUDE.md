# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Standalone scripts that back up, verify and maintain a Proton Drive library
(Drive files and the Photos section) by talking to Proton **only** through
the official `proton-drive` CLI, never through the macOS File Provider mount
(far too slow to enumerate large trees). Not an installable package: each
script is run directly (`./script.py ...`). The README documents every
script and the workflows; each script has a detailed header and `--help`.

This repo is public and MIT licensed (c) Enrico Saluzzi. Every script starts
with the `Author:` / `Copyright` / `SPDX-License-Identifier: MIT` header:
keep it on new files. Code, comments, messages and docs are in English.
Never commit personal data: emails, home paths, album names, statistics
about a real archive.

## Commands

No build, no test runner, standard library only. Minimum check after a change:

```bash
python3 -m py_compile some-script.py
./some-script.py --help
```

Running a script for real needs an authenticated Proton account and real
data. To verify a change without touching Proton, run the scripts against
synthetic data in a scratch folder, passing `--cli` a small fake
`proton-drive` executable that answers the subcommands the script uses
(`filesystem list/download/trash/rename`, `photo timeline/download/upload`,
`album list/photos/add-photo`) from a JSON state file.
`photo-redate.py --apply` also takes `--exiftool`, which can point to a stub.
Run those tests with `HOME` set to a scratch folder: the Drive scan always
writes to `~/drive-sync-logs`, where the user's real scans live.
Exercise the whole chain when a change touches a shared file format.

## Architecture

### Shared module: `protonlib.py`

Not a script: helpers used by several scripts (`human`, `sha1_of`, `nfc`,
`as_local`/`FORBIDDEN_CHARS`, `run_cli`, `DEFAULT_CLI`). Scripts do
`import protonlib`, which works from any working directory because Python
puts the running script's directory on `sys.path`. Put anything useful to
more than one script here instead of copying it.

### Data pipeline: who writes what for whom

Scripts share state through files, so order matters:

```
protonlib.scan_drive() -> ~/drive-sync-logs/<root>.tsv (path, size, mtime, sha1,
                   flag); called by file-backup.py and drive-compare.py at
                   start. Incomplete scans keep .state/.queue and resume
                   (within 24 h); complete ones remove them.

photo-backup.py -> .proton-photo-inventory.json (timeline), .proton-photo-state.txt
                   (uids downloaded), .proton-photo-map.json (uid -> local file,
                   matched by sha1), .proton-albums.json (with --albums)
                   -> photo-prune.py, photo-redate.py

photo-redate.py -> redate-plan.json -> photo-redate.py --apply (reupload-state.json)

photo-compare.py -> reads the timeline itself (`photo timeline -d -j`, saved
                   to ~/drive-sync-logs) or takes one with --inventory; it
                   doesn't depend on a photo backup
```

`photo-compare.py` matches local photos to Proton Photos by sha1, then by
capture time (Dropbox Camera Uploads file name or EXIF), one Proton photo per
local file. Matching "in any time zone" was tested on real data and rejected:
it produced coincidental matches at offsets like +2h15.

These file names and JSON keys are a contract between scripts and with
existing backups on users' disks: changing one means updating every reader,
and it breaks incremental state for existing users.

`.proton-photo-map.json` is the key piece. It's built on content (sha1), not
names, because the CLI renames duplicates and identical content can exist
under several uids; among identical copies, a photo is paired with the copy
bearing its own name. `photo-backup.py` rebuilds it on every run and keeps the
last known path of photos deleted from the backup but still on Proton:
`photo-prune.py --from-missing` relies on those paths not existing.

There are six scripts on purpose, one per user goal: `file-backup.py` and
`drive-compare.py` for Drive; `photo-backup.py`, `photo-redate.py`,
`photo-prune.py` and `photo-compare.py` for Photos. Each runs as a single
command (the Drive scan is internal). Add options to them rather than new
scripts, and put logic shared by several of them in `protonlib.py`.

### Safety conventions to preserve

These are consequences of real failures, not style:

- **Never `shell=True`, never command strings**: always argument lists,
  through `protonlib.run_cli`.
- **`stdin=subprocess.DEVNULL` on every CLI call** (`run_cli` does it): a
  command asking for confirmation otherwise hangs, or exits 0 having done
  nothing.
- **Don't trust exit codes**: verify the effect (files that appeared, their
  sha1, the new uid on the timeline), never just `returncode == 0`.
- **Verify by content (sha1)** whenever a hash is available, not by name,
  size or file count.
- **NFC only for comparing, never for calling the CLI**: Proton returns NFD
  names on macOS and answers "Node not found" to the NFC form. `nfc()` is for
  dictionary keys; keep the original path for `run_cli` arguments.
- **`as_local()`** when comparing a remote path with a local file: the CLI
  replaces `:` and other characters when writing to disk.
- **Dry-run by default**: anything that deletes or trashes requires
  `--apply`, shows the list first, and refuses to leave an album empty.
- **Resumable**: every long operation writes a state file and resumes from
  it. A new phase needs its own checkpoint.
- **Read-only unless stated**: the header's SAFETY section says what a script
  can touch; keep it accurate.
- **Uploads go through a generated script, never run by the tools**: the
  user reviews it first. Drive files are copied into the folder synced by
  the Proton Drive app (`drive-compare.py --copy-script`), never
  overwriting; photos go through `photo upload`
  (`protonlib.write_upload_script`), since Photos isn't a folder. Both
  scripts must skip files that are empty at run time (online-only
  placeholders read as 0 bytes and would be uploaded empty) and must run on
  macOS bash 3.2.
- **When in doubt, report missing**: a false "present" loses data when the
  old copy is deleted; a false "missing" costs a duplicate.
