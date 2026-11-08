"""Turn classified emails into events and application status changes, and raise the right alerts.

Gmail polling is postponed; emails arrive through `agent track import` (JSON from any connector, or
.eml files). Only ids, sender domain, date, status and extracted fields are stored: never the body.
"""

from __future__ import annotations

import email as email_lib
import email.policy
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from agent import db
from agent.discover.base import norm, norm_company, parse_dt
from agent.track.classify import Email, EmailClass, classify, is_relevant

# Higher rank wins: a rejection after an interview is recorded, an old confirmation can't undo it.
RANK = {
    "submitted": 0,
    "applied": 1,
    "no_response": 1,
    "assessment": 2,
    "interview": 3,
    "rejected": 4,
    "offer": 5,
}
ALERT_STATUSES = {"interview", "assessment", "offer", "recruiter_outreach"}
UNSURE_BELOW = 0.8


@dataclass
class IngestResult:
    seen: int = 0
    relevant: int = 0
    duplicates: int = 0
    events: int = 0
    unsure: int = 0
    alerts: list[str] = field(default_factory=list)


def _applications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.id, a.status, a.submitted_at, j.company, j.title, c.domain FROM applications a"
        " JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id"
        " WHERE a.status != 'skipped' AND a.status != 'prepared'"
    ).fetchall()


def link(email: Email, label: EmailClass, apps: list[sqlite3.Row]) -> tuple[int | None, float]:
    """Best application for this email and a match confidence (0..1)."""
    domain = email.sender_domain
    text = norm(f"{email.subject} {email.body[:3000]}")
    best, best_score = None, 0.0
    for app in apps:
        company = norm_company(app["company"])
        score = 0.0
        if app["domain"] and (domain == app["domain"] or domain.endswith("." + app["domain"])):
            score += 0.6
        if company and company in text:
            score += 0.5
        if label.company and norm_company(label.company) == company:
            score += 0.3
        title = norm(app["title"])
        if title and title in text:
            score += 0.4
        elif label.role and norm(label.role) == title:
            score += 0.2
        if score > best_score:
            best, best_score = app["id"], score
    return best, min(best_score, 1.0)


def ingest(conn: sqlite3.Connection, emails: list[Email], client=None, notify=None) -> IngestResult:
    """`notify(body, kind)` sends an alert (WhatsApp); None to only record."""
    result = IngestResult()
    apps = _applications(conn)
    domains = {a["domain"] for a in apps if a["domain"]}
    terms = {norm_company(a["company"]) for a in apps} | {norm(a["title"]) for a in apps}
    for message in emails:
        result.seen += 1
        if not is_relevant(message, domains, terms):
            continue
        result.relevant += 1
        if conn.execute("SELECT 1 FROM events WHERE gmail_msg_id = ?", (message.msg_id,)).fetchone():
            result.duplicates += 1
            continue
        label = classify(message, client)
        if label.status == "other":
            continue
        app_id, match = link(message, label, apps)
        confidence = round(min(label.confidence, match if app_id else label.confidence), 2)
        unsure = app_id is None or confidence < UNSURE_BELOW
        result.unsure += int(unsure)
        details = {"company": label.company, "role": label.role, "unsure": unsure}
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO events(application_id, type, source, gmail_msg_id, gmail_thread_id, sender_domain,"
                " confidence, occurred_at, details_json) VALUES (?, ?, 'gmail', ?, ?, ?, ?, ?, ?)",
                (
                    app_id,
                    label.status, message.msg_id, message.thread_id, message.sender_domain, confidence,
                    db.to_iso(message.date), json.dumps(details),
                ),
            )  # fmt: skip
            if app_id is not None and not unsure:
                apply_status(conn, app_id, label.status, message.date)
        result.events += 1
        # Interviews, assessments and offers alert even when unsure: a missed invite costs the most.
        if label.status in ALERT_STATUSES:
            company = label.company or _company_of(apps, app_id) or message.sender_domain
            text = {
                "interview": f"📅 *Interview* — {company}. Check your email and reply soon.",
                "assessment": f"📝 *Assessment* — {company}. Check the deadline in your email.",
                "offer": f"🎉 *Offer* — {company}!",
                "recruiter_outreach": f"👋 Recruiter message — {company}"
                + (f" ({label.role})" if label.role else ""),
            }[label.status]
            if unsure:
                text += " (match unsure)"
            result.alerts.append(text)
            if notify:
                notify(text, label.status)
    return result


def _company_of(apps, app_id) -> str | None:
    for app in apps:
        if app["id"] == app_id:
            return app["company"]
    return None


def apply_status(conn: sqlite3.Connection, app_id: int, status: str, when: datetime) -> bool:
    if status not in RANK:
        return False
    row = conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()
    stamp = db.to_iso(when)
    conn.execute(
        "UPDATE applications SET last_event_at = max(coalesce(last_event_at, ''), ?) WHERE id = ?",
        (stamp, app_id),
    )
    if status == "applied":
        conn.execute(
            "UPDATE applications SET confirmed_at = coalesce(confirmed_at, ?) WHERE id = ?", (stamp, app_id)
        )
    if RANK.get(status, 0) <= RANK.get(row["status"], 0):
        return False
    conn.execute(
        "UPDATE applications SET status = ?, follow_up_due = NULL WHERE id = ?",
        (status, app_id),
    )
    return True


# ---------------------------------------------------------------- import formats


def _from_dict(item: dict) -> Email:
    date = parse_dt(item.get("date") or item.get("internalDate")) or datetime.now(UTC)
    return Email(
        msg_id=str(item.get("id") or item.get("message_id") or item["msg_id"]),
        thread_id=item.get("thread_id") or item.get("threadId"),
        sender=item.get("from") or item.get("sender") or "",
        subject=item.get("subject") or "",
        date=date,
        body=item.get("body") or item.get("text") or item.get("snippet") or "",
    )


def _from_eml(path: Path) -> Email:
    msg = email_lib.message_from_bytes(path.read_bytes(), policy=email_lib.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    try:
        date = parsedate_to_datetime(msg["date"]) if msg["date"] else datetime.now(UTC)
    except (TypeError, ValueError):
        date = datetime.now(UTC)
    return Email(
        msg_id=str(msg["message-id"] or path.name),
        sender=str(msg["from"] or ""),
        subject=str(msg["subject"] or ""),
        date=date if date.tzinfo else date.replace(tzinfo=UTC),
        body=body,
    )


def load(path: Path) -> list[Email]:
    """A JSON file (list of {id, from, subject, date, body}), one .eml file, or a folder of .eml files."""
    if path.is_dir():
        return [_from_eml(p) for p in sorted(path.glob("*.eml"))]
    if path.suffix.lower() == ".eml":
        return [_from_eml(path)]
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("messages", data) if isinstance(data, dict) else data
    return [_from_dict(item) for item in items]


# ---------------------------------------------------------------- follow-ups


def followups(conn: sqlite3.Connection, now: datetime, no_response_days: int = 21) -> dict[str, list[str]]:
    """Day 7 and 14: nudge you to follow up. Day 21 with no reply: status no_response."""
    out: dict[str, list[str]] = {"nudge": [], "no_response": []}
    rows = conn.execute(
        "SELECT a.id, a.submitted_at, a.last_event_at, a.follow_up_due, j.company, j.title FROM applications a"
        " JOIN jobs j ON j.id = a.job_id WHERE a.status IN ('submitted', 'applied') AND a.submitted_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        submitted = db.parse_iso(row["submitted_at"])
        age = (now - submitted).days
        replied = row["last_event_at"] and db.parse_iso(row["last_event_at"]) > submitted + timedelta(
            minutes=60
        )
        if replied:
            continue
        if age >= no_response_days:
            conn.execute("UPDATE applications SET status = 'no_response' WHERE id = ?", (row["id"],))
            out["no_response"].append(f"{row['title']} — {row['company']}")
            continue
        due = db.parse_iso(row["follow_up_due"]) if row["follow_up_due"] else submitted + timedelta(days=7)
        if now >= due and age in range(7, no_response_days):
            out["nudge"].append(f"{row['title']} — {row['company']} ({age} days)")
            next_due = submitted + timedelta(days=14 if age < 14 else no_response_days)
            conn.execute(
                "UPDATE applications SET follow_up_due = ? WHERE id = ?", (db.to_iso(next_due), row["id"])
            )
    return out


def record_external(
    conn: sqlite3.Connection, company: str, title: str, url: str | None, when: datetime, now: datetime
) -> int:
    """An application you sent yourself: a job row (source 'manual') plus an external application."""
    from agent.discover.base import RawJob, upsert

    job_id, _ = upsert(
        conn, RawJob("manual", f"{norm_company(company)}|{norm(title)}", company, title, url=url), now
    )
    cur = conn.execute(
        "INSERT INTO applications(job_id, status, origin, submitted_at, follow_up_due)"
        " VALUES (?, 'applied', 'external', ?, ?)",
        (job_id, db.to_iso(when), db.to_iso(when + timedelta(days=7))),
    )
    return cur.lastrowid
