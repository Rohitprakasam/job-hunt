"""Nightly backup: a consistent SQLite copy plus a zip of the personal config files. Keeps 14 of each.

Restore = stop the agent, copy the .db back to %LOCALAPPDATA%\\job-agent\\data\\agent.db, unzip configs.
"""

from __future__ import annotations

import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from agent import db, paths

PERSONAL_CONFIGS = ("profile.yaml", "search.yaml", "companies.yaml", "settings.yaml", "contacts.csv")


def _prune(folder: Path, pattern: str, keep: int) -> None:
    for old in sorted(folder.glob(pattern))[:-keep]:
        old.unlink(missing_ok=True)


def nightly(conn: sqlite3.Connection, keep: int = 14, now: datetime | None = None) -> tuple[Path, Path]:
    now = now or datetime.now(UTC)
    folder = paths.backups_dir()
    db_copy = db.backup(conn, "nightly")
    archive = folder / f"config-{now:%Y%m%dT%H%M%S}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in PERSONAL_CONFIGS:
            source = paths.config_dir() / name
            if source.exists():
                zf.write(source, name)
    _prune(folder, "agent-*-nightly.db", keep)
    _prune(folder, "config-*.zip", keep)
    return db_copy, archive
