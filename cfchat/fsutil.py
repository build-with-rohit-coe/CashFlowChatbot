"""Filesystem helpers that behave the same on Windows and POSIX.

Two things differ enough between platforms to need their own code:

1. Renaming onto an open file. POSIX allows it; Windows raises WinError 32
   (PermissionError). Every rename here goes through `atomic_replace`.
2. SQLite URI filenames. A Windows path dropped straight into a URI produces
   `file:C:\\Users\\me\\db.sqlite`, and SQLite's URI parser wants forward
   slashes with percent-encoded specials. `sqlite_ro_uri` builds it properly.
"""
from __future__ import annotations

import gc
import os
import shutil
import time
from pathlib import Path


def atomic_replace(tmp: Path, dest: Path, attempts: int = 5) -> None:
    """Move `tmp` onto `dest`, retrying through transient Windows locks.

    Even with every handle of ours closed, an antivirus scanner or an
    indexer can hold a file for a moment, so a short backoff fixes what
    would otherwise be a hard failure.
    """
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            gc.collect()      # release any object still awaiting finalisation
            os.replace(tmp, dest)
            return
        except OSError as exc:            # PermissionError (WinError 32) included
            last = exc
            time.sleep(0.2 * (attempt + 1))

    # Fall back to overwriting the destination's bytes. Not atomic, but it works
    # when dest is merely open for reading rather than exclusively locked.
    try:
        with open(tmp, "rb") as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
        tmp.unlink(missing_ok=True)
        return
    except OSError:
        pass

    raise RuntimeError(
        f"Couldn't move the new file into place: {last}\n"
        f"Something still has {dest.name} open. Close any other copy of this app "
        f"(a second `streamlit run app.py`, a Python shell, or the workbook open in "
        f"Excel), then try again. If it persists, delete the cache folder:\n"
        f"  {dest.parent}"
    ) from last


def temp_sibling(dest: Path, tag: str) -> Path:
    """A scratch path next to `dest`, unique per process so a locked leftover
    from a crashed run can't block the next one."""
    return dest.with_name(f"{dest.name}.{tag}-{os.getpid()}")


def sqlite_ro_uri(db_path: Path) -> str:
    """Read-only SQLite URI that is correct on Windows as well as POSIX.

    `Path.as_uri()` gives `file:///C:/Users/me/db.sqlite` - forward slashes,
    percent-encoded specials - which is what SQLite's URI parser expects.
    """
    return f"{Path(db_path).resolve().as_uri()}?mode=ro"
