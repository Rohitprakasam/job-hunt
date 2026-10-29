"""Stop applying: a STOP file in the repo, the local apply_paused flag, or job_agent.control.stop in Mongo.
Phase 4 must call `is_stopped()` before every submit."""

from __future__ import annotations

import sqlite3

from agent import db, paths
from agent.cloud import Cloud

CACHE_FLAG = "control_stop_cached"


def is_stopped(conn: sqlite3.Connection, cloud: Cloud | None = None) -> bool:
    if paths.stop_file().exists() or db.get_flag(conn, "apply_paused") == "1":
        return True
    if cloud is not None:
        remote = cloud.read_control()
        if remote is not None:
            db.set_flag(conn, CACHE_FLAG, "1" if remote else "0")
            return remote
    # Mongo unreachable (or not configured): use the last value we saw.
    return db.get_flag(conn, CACHE_FLAG) == "1"
