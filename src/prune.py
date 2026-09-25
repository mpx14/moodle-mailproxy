#!/usr/bin/env python3
"""
moodle-mailproxy — archive pruner

Run periodically (hourly via systemd timer). Enforces two ceilings on the
archive directory:

  1. Age:  delete any .eml older than MAX_AGE_DAYS.
  2. Size: if the archive still exceeds MAX_TOTAL_BYTES afterwards,
           delete oldest files first until it's under the cap.

Then remove any directories left empty by the deletions.
"""

import logging
import os
import sys
import time
from pathlib import Path

ARCHIVE_ROOT = Path("/var/log/moodle-mailproxy/archive")
MAX_AGE_DAYS = 14
MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB

log = logging.getLogger("moodle-mailproxy-prune")


def list_eml_files():
    """Return list of (mtime, size, path) for every .eml under ARCHIVE_ROOT."""
    out = []
    for p in ARCHIVE_ROOT.rglob("*.eml"):
        try:
            st = p.stat()
        except FileNotFoundError:
            continue  # raced with another process; ignore
        out.append((st.st_mtime, st.st_size, p))
    return out


def remove_empty_dirs():
    """
    Walk ARCHIVE_ROOT bottom-up, removing any empty directory except
    the root itself. Idempotent and safe to run on every invocation.
    """
    for dirpath, dirnames, filenames in os.walk(ARCHIVE_ROOT, topdown=False):
        d = Path(dirpath)
        if d == ARCHIVE_ROOT:
            continue
        try:
            d.rmdir()  # only succeeds if empty
        except OSError:
            pass  # not empty, leave it


def prune_by_age(files, cutoff_mtime):
    """Delete files older than cutoff_mtime. Returns (deleted_count, freed_bytes)."""
    deleted, freed = 0, 0
    for mtime, size, path in files:
        if mtime < cutoff_mtime:
            try:
                path.unlink()
                deleted += 1
                freed += size
            except FileNotFoundError:
                pass
    return deleted, freed


def prune_by_size(files, max_bytes):
    """
    Delete oldest-first until total size <= max_bytes.
    Returns (deleted_count, freed_bytes).
    """
    # Sort oldest-first
    survivors = sorted(files, key=lambda t: t[0])
    total = sum(size for _, size, _ in survivors)
    deleted, freed = 0, 0
    for mtime, size, path in survivors:
        if total <= max_bytes:
            break
        try:
            path.unlink()
            deleted += 1
            freed += size
            total -= size
        except FileNotFoundError:
            pass
    return deleted, freed


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if not ARCHIVE_ROOT.exists():
        log.warning("archive root %s does not exist, nothing to do", ARCHIVE_ROOT)
        return 0

    cutoff = time.time() - (MAX_AGE_DAYS * 86400)

    # Pass 1: age
    files = list_eml_files()
    age_count, age_freed = prune_by_age(files, cutoff)

    # Pass 2: size (re-list, because pass 1 deleted files)
    files = list_eml_files()
    size_count, size_freed = prune_by_size(files, MAX_TOTAL_BYTES)

    remove_empty_dirs()

    # Final stats
    files = list_eml_files()
    total_after = sum(size for _, size, _ in files)
    log.info(
        "pruned: age=%d files / %d bytes, size=%d files / %d bytes; "
        "archive now %d files / %d bytes (cap %d)",
        age_count, age_freed,
        size_count, size_freed,
        len(files), total_after, MAX_TOTAL_BYTES,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
