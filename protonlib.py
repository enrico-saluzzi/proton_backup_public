#!/usr/bin/env python3
# Author: Enrico Saluzzi
# Copyright (c) 2026 Enrico Saluzzi
# SPDX-License-Identifier: MIT
"""
protonlib.py - Shared helpers used by the other scripts in this repository.

Not a standalone script: the other scripts import it. Python automatically
adds the directory of the script being run to sys.path, so `import protonlib`
works with no setup from any working directory, as long as this file sits
next to the script that imports it.
"""

import hashlib
import os
import shlex
import subprocess
import time
import unicodedata

DEFAULT_CLI = "proton-drive"


def human(n):
    """Format a byte count as a human-readable size (KB, MB, ...)."""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def sha1_of(path, chunk=1 << 20):
    """Sha1 of a file's content, or None if it can't be read."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(chunk), b""):
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def nfc(s):
    """Normalize to NFC for comparisons only.

    On macOS Proton returns names in NFD, and passing the NFC form back to
    the CLI gets you "Node not found". Use this for dictionary keys when
    comparing, never for arguments passed to run_cli().
    """
    return unicodedata.normalize("NFC", s)


# macOS doesn't allow ':' in filenames (an HFS-era leftover, where it was the
# path separator). The CLI replaces it with '_' when it writes a file to disk,
# and does the same with a few other problematic characters. Without this
# normalization, the same file looks both "missing" and "orphaned" when a
# remote inventory is compared with a local backup.
FORBIDDEN_CHARS = {":": "_", "|": "_", "\r": "_", "\n": "_", "*": "_",
                   "?": "_", "<": "_", ">": "_", '"': "_"}


def as_local(rel):
    """Apply to a remote path the same character substitution the CLI
    applies when it writes the file to disk."""
    for a, b in FORBIDDEN_CHARS.items():
        rel = rel.replace(a, b)
    return rel


def run_cli(cli, cmd, timeout, capture=True):
    """Invoke the Proton Drive CLI with stdin closed.

    Without this, a command that asks for confirmation waits on stdin until
    the timeout, or exits 0 without having done anything.
    """
    return subprocess.run([cli] + cmd, capture_output=capture, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout)


# Upload scripts are generated, never run directly by these tools: the user
# reviews them first. They must work with the bash 3.2 shipped with macOS
# (no `set -u` with arrays, no associative arrays).
_UPLOAD_SCRIPT = """#!/bin/bash
{header}
#
# Review this file before running it: nothing has been uploaded yet.
# Files that are empty at run time are skipped: an online-only placeholder
# (0 bytes) would otherwise be uploaded as an empty file. Make the source
# folders available offline first.

CLI={cli}
uploaded=0; skipped=0; failed=0
{functions}
# put DESTINATION FILE...: upload the non-empty files in one CLI call
put() {{
    dest="$1"; shift
    files=()
    for f in "$@"; do
        if [ -s "$f" ]; then
            files+=("$f")
        else
            echo "  skipped, empty or missing (online-only?): $f"
            skipped=$((skipped + 1))
        fi
    done
    [ ${{#files[@]}} -eq 0 ] && return 0
    echo "  uploading ${{#files[@]}} file(s) -> $dest"
    if {upload_cmd} < /dev/null; then
        uploaded=$((uploaded + ${{#files[@]}}))
    else
        echo "  FAILED: upload -> $dest"
        failed=$((failed + ${{#files[@]}}))
    fi
}}

{calls}

echo ""
echo "Uploaded: $uploaded   Skipped: $skipped   Failed: $failed"
echo ""
echo "A zero exit code from the CLI doesn't prove the files arrived."
echo "Verify by running the comparison again:"
{verify}
[ "$failed" -eq 0 ]
"""


def write_upload_script(path, cli, header, upload_cmd, calls, verify, functions=""):
    """Write an executable bash script that uploads files with the CLI.

    header: comment lines describing what the script does.
    upload_cmd: the bash command run by put(); it can use "$CLI",
        "${files[@]}" (the non-empty files) and "$dest".
    calls: list of argument lists; each becomes `put DEST FILE...` (or a
        raw line if it's a string, e.g. a call to a function in `functions`).
    verify: shell commands to show for verifying the result afterwards.
    """
    lines = []
    for c in calls:
        lines.append(c if isinstance(c, str) else
                     "put " + " ".join(shlex.quote(x) for x in c))
    text = _UPLOAD_SCRIPT.format(
        header="\n".join("# " + h if h else "#" for h in header),
        cli=shlex.quote(cli),
        functions=functions,
        upload_cmd=upload_cmd,
        calls="\n".join(lines),
        verify="\n".join("echo " + shlex.quote("    " + v) for v in verify),
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o755)


def generated_on():
    return time.strftime("%Y-%m-%d %H:%M")
