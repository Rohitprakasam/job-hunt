"""Free-tier request quotas. Gemini limits are per model, per minute and per day (day resets at
midnight America/Los_Angeles). Counts come from llm_calls rows with sent=1."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent import db
from agent.config import ModelLimit

PACIFIC = ZoneInfo("America/Los_Angeles")


class QuotaRefused(Exception):
    def __init__(self, model: str, reason: str):
        super().__init__(f"{model}: {reason}")
        self.model = model
        self.reason = reason


def exhausted_key(model: str) -> str:
    return f"quota_exhausted:{model}"


def pacific_day_start(now: datetime) -> datetime:
    local = now.astimezone(PACIFIC)
    return datetime.combine(local.date(), time.min, tzinfo=PACIFIC)


def next_pacific_midnight(now: datetime) -> datetime:
    local = now.astimezone(PACIFIC)
    return datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=PACIFIC)


def _count_since(conn: sqlite3.Connection, model: str, since: datetime) -> int:
    row = conn.execute(
        "SELECT count(*) FROM llm_calls WHERE model = ? AND sent = 1 AND created_at >= ?",
        (model, db.to_iso(since)),
    ).fetchone()
    return row[0]


def used_today(conn: sqlite3.Connection, model: str, now: datetime) -> int:
    return _count_since(conn, model, pacific_day_start(now))


def exhausted_until(conn: sqlite3.Connection, model: str, now: datetime) -> datetime | None:
    value = db.get_flag(conn, exhausted_key(model))
    if value:
        until = db.parse_iso(value)
        if until > now:
            return until
    return None


def check(conn: sqlite3.Connection, model: str, limit: ModelLimit, headroom: float, now: datetime) -> None:
    if exhausted_until(conn, model, now):
        raise QuotaRefused(model, "daily quota exhausted")
    if _count_since(conn, model, now - timedelta(seconds=60)) >= max(1, int(limit.rpm * headroom)):
        raise QuotaRefused(model, "per-minute limit")
    if used_today(conn, model, now) >= max(1, int(limit.rpd * headroom)):
        raise QuotaRefused(model, "daily limit")


def mark_exhausted(conn: sqlite3.Connection, model: str, now: datetime) -> datetime:
    until = next_pacific_midnight(now)
    db.set_flag(conn, exhausted_key(model), db.to_iso(until))
    return until
