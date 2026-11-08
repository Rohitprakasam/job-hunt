"""One `runs` row per stage execution, so the dashboard and weekly review can measure pipeline health."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from agent import db

log = logging.getLogger(__name__)


@dataclass
class Run:
    id: int
    stage: str
    items: int = 0
    error: str | None = None


@contextmanager
def stage_run(conn: sqlite3.Connection, stage: str) -> Iterator[Run]:
    """Records start, finish, item count and ok/error. Exceptions are recorded, then re-raised."""
    cur = conn.execute("INSERT INTO runs(stage, started_at) VALUES (?, ?)", (stage, db.utcnow()))
    run = Run(id=cur.lastrowid, stage=stage)
    try:
        yield run
    except BaseException as exc:
        conn.execute(
            "UPDATE runs SET finished_at = ?, ok = 0, items = ?, error = ? WHERE id = ?",
            (db.utcnow(), run.items, type(exc).__name__, run.id),
        )
        raise
    conn.execute(
        "UPDATE runs SET finished_at = ?, ok = ?, items = ?, error = ? WHERE id = ?",
        (db.utcnow(), int(run.error is None), run.items, run.error, run.id),
    )


def last_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT ok FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return bool(row["ok"]) if row else True
