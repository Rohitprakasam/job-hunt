import sqlite3

import pytest

from agent import db, paths


def test_fresh_migrate(tmp_path):
    conn = db.connect(tmp_path / "a.db")
    result = db.migrate(conn)
    assert (result.from_version, result.to_version) == (0, db.latest_version())
    assert result.backup is None
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_twice_is_noop(conn):
    result = db.migrate(conn)
    assert result.from_version == result.to_version == db.latest_version()


def test_foreign_key_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO applications(job_id, status, origin) VALUES (999, 'prepared', 'agent')")


def test_status_check_enforced(conn):
    conn.execute(
        "INSERT INTO jobs(source, company, title, first_seen_at, dedupe_key) VALUES ('t', 'c', 't', ?, 'k')",
        (db.utcnow(),),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO applications(job_id, status, origin) VALUES (1, 'approved', 'agent')")


def test_backup_before_migrating_non_empty_db(tmp_path):
    conn = db.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE legacy(x)")
    result = db.migrate(conn)
    assert result.backup is not None and result.backup.exists()
    assert result.backup.parent == paths.backups_dir()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    bad = tmp_path / "migrations"
    bad.mkdir()
    (bad / "0001_bad.sql").write_text("CREATE TABLE ok(x);\nCREATE TABLE broken(;\n")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", bad)
    conn = db.connect(tmp_path / "b.db")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn)
    assert db.current_version(conn) == 0
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='ok'").fetchone()[0] == 0


def test_flags_roundtrip(conn):
    assert db.get_flag(conn, "x") is None
    db.set_flag(conn, "x", "1")
    db.set_flag(conn, "x", "2")
    assert db.get_flag(conn, "x") == "2"
    db.clear_flag(conn, "x")
    assert db.get_flag(conn, "x") is None


def test_iso_roundtrip():
    stamp = db.utcnow()
    assert stamp.endswith("Z")
    assert db.to_iso(db.parse_iso(stamp)) == stamp
