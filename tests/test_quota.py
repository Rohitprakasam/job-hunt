from datetime import UTC, datetime, timedelta

import pytest

from agent import db, quota
from agent.config import ModelLimit

MODEL = "gemini-test"


def _calls(conn, n, when, sent=1):
    for _ in range(n):
        conn.execute(
            "INSERT INTO llm_calls(stage, tier, model, sent, ok, created_at) VALUES ('t', 'fast', ?, ?, 1, ?)",
            (MODEL, sent, db.to_iso(when)),
        )


def test_pacific_midnight_standard_and_daylight_time():
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert quota.next_pacific_midnight(winter).astimezone(UTC) == datetime(2026, 1, 16, 8, 0, tzinfo=UTC)
    assert quota.next_pacific_midnight(summer).astimezone(UTC) == datetime(2026, 7, 16, 7, 0, tzinfo=UTC)


def test_dst_change_day():
    # 2026-11-01 is the US fall-back day; the next midnight is 25 hours after the previous one.
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    start = quota.pacific_day_start(now)
    # Subtract in UTC: same-zone aware datetimes subtract by wall clock and would hide the extra hour.
    assert quota.next_pacific_midnight(now).astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=25)


def test_refuses_at_headroom_per_minute(conn):
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    _calls(conn, 9, now - timedelta(seconds=10))
    with pytest.raises(quota.QuotaRefused, match="per-minute"):
        quota.check(conn, MODEL, ModelLimit(rpm=10, rpd=1000), 0.9, now)


def test_refuses_at_headroom_per_day(conn):
    now = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
    _calls(conn, 90, now - timedelta(hours=2))
    with pytest.raises(quota.QuotaRefused, match="daily limit"):
        quota.check(conn, MODEL, ModelLimit(rpm=1000, rpd=100), 0.9, now)


def test_unsent_calls_do_not_count(conn):
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    _calls(conn, 50, now - timedelta(seconds=5), sent=0)
    quota.check(conn, MODEL, ModelLimit(rpm=10, rpd=100), 0.9, now)


def test_yesterday_does_not_count(conn):
    now = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)  # 13:00 Pacific
    _calls(conn, 95, now - timedelta(hours=14))  # previous Pacific day
    quota.check(conn, MODEL, ModelLimit(rpm=1000, rpd=100), 0.9, now)


def test_mark_exhausted_until_next_midnight(conn):
    now = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
    until = quota.mark_exhausted(conn, MODEL, now)
    assert quota.exhausted_until(conn, MODEL, now) == until
    assert quota.exhausted_until(conn, MODEL, until + timedelta(seconds=1)) is None
    with pytest.raises(quota.QuotaRefused, match="exhausted"):
        quota.check(conn, MODEL, ModelLimit(rpm=10, rpd=100), 0.9, now)
