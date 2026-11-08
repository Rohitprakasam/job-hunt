"""Dashboard snapshot: counts and fixed codes only, built from an allow-list.

No company names, job titles, URLs, email text, resume text, answers, cookies or tokens can appear:
every key and every string value is checked against fixed lists, and anything else raises.
The same schema is enforced again by the Mongo validator and by the Vercel function.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from agent import __version__, db
from agent.runs import last_ok

SCHEMA_VERSION = 1
SOURCES = (
    "greenhouse",
    "lever",
    "ashby",
    "linkedin",
    "indeed",
    "naukri",
    "glassdoor",
    "google",
    "zip_recruiter",
    "manual",
)
STAGES = ("discover", "score", "prepare", "apply", "track", "notify")
STAGE_STATES = ("ok", "error", "idle", "paused")
ERROR_CODES = (
    "scrape_blocked",
    "source_failed",
    "form_fill_failed",
    "apply_blocked",
    "llm_quota",
    "render_failed",
)
FUNNEL = ("applied", "responded", "assessment", "interview", "offer", "rejected", "no_response")
COUNTS = ("found", "new_after_dedupe", "matched", "prepared", "applied")

ALLOWED: dict[str, Any] = {
    "schema": int,
    "sent_at": str,
    "worker": {"version": str, "uptime_s": int, "last_run_ok": bool},
    "stages": {s: STAGE_STATES for s in STAGES},
    "counts_today": {k: int for k in COUNTS},
    "funnel_30d": {k: int for k in FUNNEL},
    "sources": [{"name": SOURCES, "jobs": int, "errors": int}],
    "errors_24h": {k: int for k in ERROR_CODES},
}


class SanitizeError(ValueError):
    pass


def validate(payload: Any, spec: Any = ALLOWED, where: str = "$") -> None:
    if isinstance(spec, dict):
        if not isinstance(payload, dict):
            raise SanitizeError(f"{where}: expected object")
        extra = set(payload) - set(spec)
        if extra:
            raise SanitizeError(f"{where}: keys not allowed: {sorted(extra)}")
        missing = set(spec) - set(payload)
        if missing:
            raise SanitizeError(f"{where}: missing keys: {sorted(missing)}")
        for key, sub in spec.items():
            validate(payload[key], sub, f"{where}.{key}")
    elif isinstance(spec, list):
        if not isinstance(payload, list) or len(payload) > len(SOURCES):
            raise SanitizeError(f"{where}: expected a short list")
        for i, item in enumerate(payload):
            validate(item, spec[0], f"{where}[{i}]")
    elif isinstance(spec, tuple):
        if payload not in spec:
            raise SanitizeError(f"{where}: value not in the fixed list")
    elif spec is bool:
        if not isinstance(payload, bool):
            raise SanitizeError(f"{where}: expected bool")
    elif spec is int:
        if isinstance(payload, bool) or not isinstance(payload, int) or payload < 0:
            raise SanitizeError(f"{where}: expected a non-negative integer")
    elif spec is str:
        # Free strings are allowed only for the timestamp and version, and are shape-checked.
        if not isinstance(payload, str) or len(payload) > 32:
            raise SanitizeError(f"{where}: expected a short string")
        if where == "$.sent_at":
            db.parse_iso(payload)
        elif where == "$.worker.version" and not all(c.isdigit() or c == "." for c in payload):
            raise SanitizeError(f"{where}: bad version")


def _count(conn: sqlite3.Connection, sql: str, *args: Any) -> int:
    return int(conn.execute(sql, args).fetchone()[0] or 0)


def _stage_state(conn: sqlite3.Connection, stage: str, now: datetime) -> str:
    row = conn.execute(
        "SELECT ok, finished_at FROM runs WHERE stage = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (stage,),
    ).fetchone()
    if row is None or db.parse_iso(row["finished_at"]) < now - timedelta(hours=26):
        return "idle"
    return "ok" if row["ok"] else "error"


def build(conn: sqlite3.Connection, now: datetime, uptime_s: int, apply_paused: bool) -> dict[str, Any]:
    day = db.to_iso(now - timedelta(hours=24))
    month = db.to_iso(now - timedelta(days=30))
    stages = {s: _stage_state(conn, s, now) for s in STAGES}
    if apply_paused:
        stages["apply"] = "paused"
    statuses = dict(
        conn.execute(
            "SELECT status, count(*) FROM applications WHERE submitted_at >= ? GROUP BY status", (month,)
        ).fetchall()
    )
    applied = sum(v for k, v in statuses.items() if k not in ("prepared", "skipped"))
    funnel = {
        "applied": applied,
        "responded": sum(statuses.get(k, 0) for k in ("assessment", "interview", "offer", "rejected")),
        **{
            k: int(statuses.get(k, 0))
            for k in ("assessment", "interview", "offer", "rejected", "no_response")
        },
    }
    sources = []
    for name in SOURCES:
        jobs = _count(conn, "SELECT count(*) FROM jobs WHERE source = ? AND first_seen_at >= ?", name, day)
        errors = _count(
            conn,
            "SELECT coalesce(sum(consecutive_failures), 0) FROM source_health WHERE source = ? OR source LIKE ?",
            name, f"{name}:%",
        )  # fmt: skip
        if jobs or errors:
            sources.append({"name": name, "jobs": jobs, "errors": errors})
    errors_24h = {code: 0 for code in ERROR_CODES}
    errors_24h["source_failed"] = _count(
        conn, "SELECT count(*) FROM source_health WHERE disabled_until > ?", db.to_iso(now)
    )
    errors_24h["apply_blocked"] = _count(
        conn, "SELECT count(*) FROM apply_attempts WHERE outcome = 'blocked' AND started_at >= ?", day
    )
    errors_24h["form_fill_failed"] = _count(
        conn, "SELECT count(*) FROM apply_attempts WHERE outcome = 'failed' AND started_at >= ?", day
    )
    errors_24h["llm_quota"] = _count(
        conn, "SELECT count(*) FROM llm_calls WHERE error_code LIKE 'rate_limited%' AND created_at >= ?", day
    )
    errors_24h["render_failed"] = _count(
        conn, "SELECT count(*) FROM applications a JOIN jobs j ON j.id = a.job_id"
        " WHERE a.skip_reason LIKE 'render:%' AND j.first_seen_at >= ?", day,
    )  # fmt: skip
    payload = {
        "schema": SCHEMA_VERSION,
        "sent_at": db.to_iso(now),
        "worker": {"version": __version__, "uptime_s": int(uptime_s), "last_run_ok": last_ok(conn)},
        "stages": stages,
        "counts_today": {
            "found": _count(conn, "SELECT count(*) FROM jobs WHERE last_seen_at >= ?", day),
            "new_after_dedupe": _count(conn, "SELECT count(*) FROM jobs WHERE first_seen_at >= ?", day),
            "matched": _count(
                conn,
                "SELECT count(*) FROM scores s JOIN jobs j ON j.id = s.job_id WHERE j.first_seen_at >= ?"
                " AND s.fit >= 55",
                day,
            ),  # fmt: skip
            "prepared": _count(conn, "SELECT count(*) FROM applications WHERE status = 'prepared'"),
            "applied": _count(conn, "SELECT count(*) FROM applications WHERE submitted_at >= ?", day),
        },
        "funnel_30d": funnel,
        "sources": sources,
        "errors_24h": errors_24h,
    }
    validate(payload)
    return payload
