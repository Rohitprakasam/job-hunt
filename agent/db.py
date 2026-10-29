"""SQLite access, numbered migrations (tracked by PRAGMA user_version) and the flags table."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent import paths

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[\w-]+\.sql$")


def to_iso(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def utcnow() -> str:
    return to_iso(datetime.now(UTC))


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Autocommit connection; use `transaction()` for multi-statement writes."""
    conn = sqlite3.connect(path or paths.db_path(), isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ---------------------------------------------------------------- migrations


def available_migrations() -> list[tuple[int, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    return found


def latest_version() -> int:
    migrations = available_migrations()
    return migrations[-1][0] if migrations else 0


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


@dataclass
class MigrationResult:
    from_version: int
    to_version: int
    backup: Path | None


def _has_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0


def backup(conn: sqlite3.Connection, label: str) -> Path:
    target = paths.backups_dir() / f"agent-{datetime.now(UTC):%Y%m%dT%H%M%S}-{label}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(conn: sqlite3.Connection) -> MigrationResult:
    start = current_version(conn)
    pending = [(v, p) for v, p in available_migrations() if v > start]
    if not pending:
        return MigrationResult(start, start, None)
    backup_path = backup(conn, f"v{start}") if _has_tables(conn) else None
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        # executescript would COMMIT first, so run statements one by one inside one transaction.
        with transaction(conn):
            for statement in _split_sql(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version:d}")
    return MigrationResult(start, current_version(conn), backup_path)


def _split_sql(sql: str) -> list[str]:
    statements, buffer = [], ""
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError("migration ends with an incomplete statement")
    return statements


# ---------------------------------------------------------------- flags


def get_flag(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM flags WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_flag(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO flags(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utcnow()),
    )


def clear_flag(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM flags WHERE key = ?", (key,))


def info(conn: sqlite3.Connection) -> dict[str, object]:
    tables = [
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    db_file = Path(conn.execute("PRAGMA database_list").fetchone()["file"] or ":memory:")
    return {
        "path": str(db_file),
        "size_bytes": db_file.stat().st_size if db_file.exists() else 0,
        "user_version": current_version(conn),
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "rows": counts,
    }
