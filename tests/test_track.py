import json
from datetime import UTC, datetime, timedelta

import pytest

from agent.discover.base import RawJob, upsert
from agent.track import classify, ingest

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)


def _email(subject, body, sender="no-reply@greenhouse.io", msg_id=None):
    return classify.Email(msg_id or subject, sender, subject, NOW, body)


@pytest.mark.parametrize(
    "subject, body, status",
    [
        ("Thank you for applying to Acme", "We have received your application.", "applied"),
        (
            "Your application",
            "Unfortunately, we have decided to move forward with other candidates.",
            "rejected",
        ),
        (
            "Next steps",
            "We'd like to schedule an interview with you. Book a time via calendly.com/acme",
            "interview",
        ),
        ("Coding challenge", "Please complete the HackerRank online assessment within 3 days.", "assessment"),
        ("Offer", "We are pleased to offer you the position. Your offer letter is attached.", "offer"),
    ],
)
def test_rules(subject, body, status):
    assert classify.classify(_email(subject, body)).status == status


def test_low_confidence_interview_still_flags():
    label = classify.classify(_email("Quick question", "Are you free for an interview chat sometime?"))
    assert label.status == "interview" and label.confidence < 0.8


def test_trimmed_body_cuts_quotes():
    body = "Hello, see you Monday.\n\nOn Mon, Oct 27, 2026 at 9:00 Rohit wrote:\n> secret quoted text"
    assert "secret" not in classify.trimmed_body(body)


def test_relevance_filter():
    assert classify.is_relevant(_email("x", "y", sender="Acme <jobs@acme.com>"), {"acme.com"}, set())
    assert not classify.is_relevant(
        _email("Sale!", "50% off", sender="deals@shop.com"), {"acme.com"}, {"acme"}
    )
    assert classify.is_relevant(_email("Re: Acme application", "", sender="hr@gmail.com"), set(), {"acme"})


@pytest.fixture
def submitted(conn):
    job_id, _ = upsert(conn, RawJob("greenhouse", "1", "Acme", "Security Analyst"), NOW)
    cur = conn.execute(
        "INSERT INTO applications(job_id, status, origin, submitted_at) VALUES (?, 'submitted', 'agent', ?)",
        (job_id, "2026-10-20T00:00:00.000Z"),
    )
    return cur.lastrowid


def test_ingest_links_updates_and_alerts(conn, submitted):
    alerts = []
    emails = [
        _email("Thank you for applying to Acme", "Security Analyst role: application received", msg_id="m1"),
        _email(
            "Acme interview",
            "Acme would like to schedule an interview for the Security Analyst role.",
            msg_id="m2",
        ),
        _email("Acme interview", "duplicate", msg_id="m2"),
    ]
    result = ingest.ingest(conn, emails, notify=lambda body, kind: alerts.append((kind, body)))
    assert (result.events, result.duplicates) == (2, 1)
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (submitted,)).fetchone()
    assert app["status"] == "interview" and app["confirmed_at"] is not None
    assert alerts and alerts[0][0] == "interview" and "Acme" in alerts[0][1]
    # A late confirmation never downgrades the status.
    ingest.ingest(conn, [_email("Thanks for applying to Acme", "Security Analyst received", msg_id="m3")])
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "interview"
    assert all("body" not in json.loads(r[0]) for r in conn.execute("SELECT details_json FROM events"))


def test_followups_and_no_response(conn, submitted):
    conn.execute(
        "UPDATE applications SET submitted_at = ? WHERE id = ?", ("2026-10-21T00:00:00.000Z", submitted)
    )
    due = ingest.followups(conn, NOW)
    assert len(due["nudge"]) == 1
    assert ingest.followups(conn, NOW)["nudge"] == []  # next nudge on day 14
    assert ingest.followups(conn, NOW + timedelta(days=14))["no_response"] == ["Security Analyst — Acme"]
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "no_response"


def test_load_json_and_eml(tmp_path):
    data = [{"id": "a", "from": "x@lever.co", "subject": "s", "date": "2026-10-28T10:00:00Z", "body": "b"}]
    (tmp_path / "mail.json").write_text(json.dumps(data), encoding="utf-8")
    assert ingest.load(tmp_path / "mail.json")[0].sender_domain == "lever.co"
    eml = "From: Jobs <jobs@acme.com>\nSubject: Hi\nMessage-ID: <1@acme>\nDate: Tue, 28 Oct 2026 10:00:00 +0000\n\nBody text\n"
    (tmp_path / "a.eml").write_text(eml, encoding="utf-8")
    loaded = ingest.load(tmp_path / "a.eml")[0]
    assert (loaded.msg_id, loaded.sender_domain, loaded.body.strip()) == ("<1@acme>", "acme.com", "Body text")


def test_record_external(conn):
    app_id = ingest.record_external(conn, "Beta", "SOC Analyst", None, NOW - timedelta(days=1), NOW)
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert row["origin"] == "external" and row["status"] == "applied"
