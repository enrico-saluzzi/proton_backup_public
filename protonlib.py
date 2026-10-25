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
import subprocess
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
