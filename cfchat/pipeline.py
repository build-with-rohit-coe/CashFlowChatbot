"""Sync + build, in the right order, skipping work that isn't needed."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import CACHE_DIR, DB_PATH
from .database import build_database
from .onedrive import sync_workbook, use_local_file
from .schema import enum_values

log = logging.getLogger(__name__)
STATS_PATH = CACHE_DIR / "db_stats.json"


def ensure_ready(local_path: str | None = None, force: bool = False) -> dict:
    """Guarantee a current SQLite database exists. Returns row/date stats.

    The Excel parse takes a few seconds on 25k rows, so it only runs when the
    OneDrive file actually changed or the database is missing.
    """
    if local_path:
        xlsx, changed = use_local_file(local_path)
    else:
        xlsx, changed = sync_workbook(force=force)

    if changed or force or not DB_PATH.exists():
        log.info("Rebuilding local database from %s", xlsx.name)
        stats = build_database(xlsx)
        STATS_PATH.write_text(json.dumps(stats))
        enum_values.cache_clear() if hasattr(enum_values, "cache_clear") else None
        from .schema import _enum_values
        _enum_values.cache_clear()
    else:
        stats = json.loads(STATS_PATH.read_text()) if STATS_PATH.exists() else build_database(xlsx)

    return stats


def rebuild_from_cache() -> dict:
    """Rebuild the DB from the already-downloaded workbook. No network."""
    from .config import XLSX_CACHE

    if not Path(XLSX_CACHE).exists():
        raise FileNotFoundError("No cached workbook yet - run a sync first.")
    stats = build_database(XLSX_CACHE)
    STATS_PATH.write_text(json.dumps(stats))
    from .schema import _enum_values
    _enum_values.cache_clear()
    return stats
