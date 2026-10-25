#!/bin/bash
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
#
# protondrive-lockfile-guard.sh
#
# Watches a Proton Drive synced folder and instantly quarantines transient
# Office/LibreOffice lock files (e.g. ~$Report.xlsx, .~lock.Report.ods#)
# before Proton Drive's File Provider extension can pick them up and try
# to sync them. These files are created and deleted rapidly while a document
# is open, and were the suspected trigger for AsyncManagedObjectContextPool
# exhaustion and multi-hour sync hangs in Proton Drive for macOS 3.0.0+12452.
#
# Quarantined files are moved, never deleted, and purged automatically after
# QUARANTINE_RETENTION_DAYS. Meant to run in the background, e.g. from a
# LaunchAgent (see the PATH note below).
#
# Requires: fswatch (brew install fswatch)
#
# Usage:
#   1. edit WATCH_DIR below
#   2. ./protondrive-lockfile-guard.sh
#
# ---- CONFIGURATION ---------------------------------------------------------

# EDIT THIS: path to your Proton Drive synced folder.
# Find it with:  ls ~/Library/CloudStorage/ | grep -i proton
WATCH_DIR="$HOME/Library/CloudStorage/ProtonDrive-you@example.com-folder"

# Where quarantined lock files are moved (kept, not deleted, so nothing
# is ever truly lost: just taken out of the synced folder).
QUARANTINE_DIR="$HOME/.protondrive-lockfile-quarantine"

# Log file for visibility / debugging.
LOG_FILE="$HOME/Library/Logs/protondrive-lockfile-guard.log"

# How many days to keep quarantined files before purging them.
QUARANTINE_RETENTION_DAYS=7

# ----------------------------------------------------------------------------

mkdir -p "$QUARANTINE_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

# launchd runs this script with a minimal PATH that does NOT include
# Homebrew's bin directories, even though `fswatch` works fine from
# Terminal. Explicitly add both common Homebrew locations (Apple
# Silicon and Intel) so this works the same whether run manually or
# via LaunchAgent.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

FSWATCH_BIN="$(command -v fswatch)"

if [ -z "$FSWATCH_BIN" ]; then
    log "ERROR: fswatch not found even after adding /opt/homebrew/bin and /usr/local/bin to PATH."
    log "Run 'which fswatch' in Terminal and hardcode the full path into FSWATCH_BIN if needed."
    echo "fswatch not found. Install it with: brew install fswatch" >&2
    exit 1
fi

log "Using fswatch at: $FSWATCH_BIN"

if [ ! -d "$WATCH_DIR" ]; then
    log "ERROR: WATCH_DIR does not exist: $WATCH_DIR"
    echo "WATCH_DIR does not exist: $WATCH_DIR" >&2
    echo "Find the correct path with: ls ~/Library/CloudStorage/ | grep -i proton" >&2
    exit 1
fi

log "Starting lockfile guard on: $WATCH_DIR"

# Purge old quarantined files on startup.
find "$QUARANTINE_DIR" -type f -mtime "+${QUARANTINE_RETENTION_DAYS}" -delete 2>/dev/null

# --event Created: only react to file creation, not every modification.
# -r: recursive. -0 + read -d '': handles filenames with spaces/newlines safely.
"$FSWATCH_BIN" -r -0 --event Created "$WATCH_DIR" | while IFS= read -r -d "" path; do
    filename="$(basename "$path")"

    case "$filename" in
        '~$'*.xlsx|'~$'*.xlsm|'~$'*.xls| \
        '~$'*.docx|'~$'*.doc| \
        '~$'*.pptx|'~$'*.ppt| \
        '.~lock.'*'#')
            # Small delay so Office finishes its initial write before we move it.
            sleep 0.3
            if [ -e "$path" ]; then
                dest="$QUARANTINE_DIR/${filename}.$(date +%s)"
                if mv "$path" "$dest" 2>>"$LOG_FILE"; then
                    log "Quarantined: $path -> $dest"
                else
                    log "FAILED to quarantine: $path"
                fi
            fi
            ;;
    esac
done
