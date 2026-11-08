"""WhatsApp texts: morning digest, urgent matches, alerts. Company, role, score, link and counts only:
no resume content and no form answers ever go through WhatsApp."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent import db
from agent.config import NotifySettings, Settings

PRIORITY_URGENT = 5
PRIORITY_ALERT = 8  # interviews, offers, system failures: allowed in quiet hours


def _hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def local(now: datetime, notify: NotifySettings) -> datetime:
    return now.astimezone(ZoneInfo(notify.timezone))


def in_quiet_hours(now: datetime, notify: NotifySettings) -> bool:
    t = local(now, notify).time()
    start, end = _hhmm(notify.quiet_start), _hhmm(notify.quiet_end)
    return (t >= start or t < end) if start > end else (start <= t < end)


def _tag(
    conn: sqlite3.Connection, settings: Settings, ats_type: str | None, source: str | None = None
) -> str:
    if ats_type in ("greenhouse", "lever", "ashby"):
        try:
            from agent.apply.runner import live_allowed

            return "auto" if live_allowed(conn, settings, ats_type) else "you"
        except Exception:
            return "you"
    if source in ("linkedin", "naukri", "indeed"):
        try:
            from agent.apply.boards.base import is_site_live

            return "auto" if is_site_live(conn, settings, source) else "you"
        except Exception:
            return "you"
    return "you"


def _line(n: int, row: sqlite3.Row, tag: str = "you") -> str:
    where = f" · {row['location']}" if row["location"] else ""
    link = row["apply_url"] or row["url"] or ""
    return f"{n}. [{tag}] *{round(row['fit'])}* {row['title']} — {row['company']}{where}\n   {link}"


def top_new_matches(conn: sqlite3.Connection, settings: Settings, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT j.id, j.title, j.company, j.location, j.apply_url, j.url, j.ats_type, j.source, s.fit FROM jobs j"
        " JOIN scores s ON s.job_id = j.id WHERE j.notified_at IS NULL AND j.filter_reason IS NULL"
        " AND s.fit >= ? ORDER BY s.priority DESC LIMIT ?",
        (settings.scoring.min_digest_fit, limit),
    ).fetchall()


def yesterday_counts(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    since = db.to_iso(now - timedelta(hours=24))
    found = conn.execute("SELECT count(*) FROM jobs WHERE first_seen_at >= ?", (since,)).fetchone()[0]
    applied = conn.execute("SELECT count(*) FROM applications WHERE submitted_at >= ?", (since,)).fetchone()[
        0
    ]
    events = dict(
        conn.execute(
            "SELECT type, count(*) FROM events WHERE occurred_at >= ? GROUP BY type", (since,)
        ).fetchall()
    )
    return {"found": found, "applied": applied, **{k: int(v) for k, v in events.items()}}


def build_digest(conn: sqlite3.Connection, settings: Settings, now: datetime) -> tuple[str, list[int]]:
    rows = top_new_matches(conn, settings, settings.notify.digest_size)
    day = local(now, settings.notify).strftime("%a %d %b")
    lines = [f"🌅 *Job digest · {day}*"]
    sent_iso = db.to_iso(now)
    if rows:
        lines.append(f"Top {len(rows)} new matches:")
        for i, r in enumerate(rows, start=1):
            tag = _tag(conn, settings, r["ats_type"], r["source"])
            lines.append(_line(i, r, tag))
            try:
                conn.execute(
                    "INSERT INTO digest_items(sent_at, n, job_id) VALUES (?, ?, ?)",
                    (sent_iso, i, r["id"]),
                )
            except Exception:
                pass
        try:
            conn.commit()
        except Exception:
            pass
    else:
        lines.append("No new matches above your minimum score.")
    c = yesterday_counts(conn, now)
    replies = ", ".join(f"{k} {v}" for k, v in c.items() if k not in ("found", "applied") and v)
    lines.append(
        f"\nLast 24 h: {c['found']} jobs found · {c['applied']} applied"
        + (f" · {replies}" if replies else "")
    )
    footer = health_footer(conn, now)
    if footer:
        lines.append(footer)
    return "\n".join(lines), [r["id"] for r in rows]


def health_footer(conn: sqlite3.Connection, now: datetime) -> str:
    notes = []
    tripped = conn.execute(
        "SELECT count(*) FROM source_health WHERE disabled_until > ?", (db.to_iso(now),)
    ).fetchone()[0]
    if tripped:
        notes.append(f"{tripped} source(s) paused after errors")
    paused = db.get_flag(conn, "llm_paused_until")
    if paused and db.parse_iso(paused) > now:
        notes.append("Gemini daily quota used up")
    if db.get_flag(conn, "apply_paused") == "1":
        notes.append("applying paused")
    pending = conn.execute("SELECT count(*) FROM pending_questions").fetchone()[0]
    if pending:
        notes.append(f"{pending} question(s) need an answer (agent answers pending)")
    return "⚠️ " + "; ".join(notes) if notes else ""


def mark_notified(conn: sqlite3.Connection, job_ids: list[int], now: datetime) -> None:
    conn.executemany(
        "UPDATE jobs SET notified_at = ? WHERE id = ?", [(db.to_iso(now), job_id) for job_id in job_ids]
    )


def send_digest(conn: sqlite3.Connection, wa, settings: Settings, now: datetime) -> str:
    body, ids = build_digest(conn, settings, now)
    status = wa.send_text(body, kind="digest", priority=1)
    mark_notified(conn, ids, now)  # queued counts as delivered later: never list the same job twice
    return status


def urgent_matches(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[sqlite3.Row]:
    since = db.to_iso(now - timedelta(hours=settings.scoring.urgent_hours))
    return conn.execute(
        "SELECT j.id, j.title, j.company, j.location, j.apply_url, j.url, s.fit FROM jobs j"
        " JOIN scores s ON s.job_id = j.id WHERE j.notified_at IS NULL AND j.filter_reason IS NULL"
        " AND s.fit >= ? AND coalesce(j.posted_at, j.first_seen_at) >= ? ORDER BY s.fit DESC LIMIT 3",
        (settings.scoring.urgent_fit, since),
    ).fetchall()


def send_urgent(conn: sqlite3.Connection, wa, settings: Settings, now: datetime) -> int:
    """Strong fresh matches go out at once, except in quiet hours (then they wait for the digest)."""
    if in_quiet_hours(now, settings.notify):
        return 0
    rows = urgent_matches(conn, settings, now)
    if not rows:
        return 0
    body = (
        "🔥 *Strong new match"
        + ("es" if len(rows) > 1 else "")
        + "*\n"
        + "\n".join(_line(i, r) for i, r in enumerate(rows, start=1))
    )
    wa.send_text(body, kind="urgent", priority=PRIORITY_URGENT)
    mark_notified(conn, [r["id"] for r in rows], now)
    return len(rows)


def send_alert(wa, body: str, kind: str = "system", allow_quiet: bool = True) -> str:
    return wa.send_text(body, kind=kind, priority=PRIORITY_ALERT if allow_quiet else PRIORITY_URGENT)
