from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agent.discover.base import RawJob, upsert
from agent.notify import digest
from agent.notify.whatsapp import WhatsApp

NOW = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)  # 08:30 IST


class Cloud:
    def __init__(self, last):
        self.last = last

    def last_inbound_at(self):
        return self.last


def _wa(conn, settings, open_window=True, sent=None):
    def handler(request):
        sent.append(request.read().decode())
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    secrets = {"whatsapp_token": "t", "whatsapp_phone_number_id": "1", "whatsapp_to_number": "919000000001"}
    return WhatsApp(
        settings.whatsapp,
        conn,
        Cloud(NOW - timedelta(hours=1 if open_window else 30)),
        secret=lambda s: secrets[s.value],
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: NOW,
    )


def _scored(conn, title, fit, hours_old=2):
    job_id, _ = upsert(
        conn,
        RawJob("greenhouse", title, "Acme", title, apply_url=f"https://x/{title}", location="Chennai",
               posted_at=NOW - timedelta(hours=hours_old)),
        NOW,
    )  # fmt: skip
    conn.execute("UPDATE jobs SET filtered_at = ? WHERE id = ?", ("2026-10-29T00:00:00.000Z", job_id))
    conn.execute(
        "INSERT INTO scores(job_id, fit, scored_at, priority) VALUES (?, ?, '2026-10-29T00:00:00.000Z', ?)",
        (job_id, fit, fit),
    )
    return job_id


@pytest.mark.parametrize("hour_utc, quiet", [(17, True), (1, True), (3, False), (16, False)])
def test_quiet_hours(settings, hour_utc, quiet):
    # 22:00-07:30 IST = 16:30-02:00 UTC
    assert digest.in_quiet_hours(NOW.replace(hour=hour_utc), settings.notify) is quiet


def test_digest_lists_top_matches_once(conn, settings):
    _scored(conn, "Security Analyst", 82)
    _scored(conn, "Linux Admin", 40)  # below min_digest_fit
    sent = []
    wa = _wa(conn, settings, sent=sent)
    assert digest.send_digest(conn, wa, settings, NOW) == "sent"
    assert "Security Analyst" in sent[0] and "Linux Admin" not in sent[0] and "*82*" in sent[0]
    body, ids = digest.build_digest(conn, settings, NOW)
    assert ids == [] and "No new matches" in body


def test_urgent_respects_quiet_hours_and_marks(conn, settings):
    _scored(conn, "Security Analyst", 90, hours_old=1)
    sent = []
    wa = _wa(conn, settings, sent=sent)
    assert digest.send_urgent(conn, wa, settings, NOW.replace(hour=18)) == 0
    assert digest.send_urgent(conn, wa, settings, NOW) == 1
    assert digest.send_urgent(conn, wa, settings, NOW) == 0
    assert len(sent) == 1


def test_queue_flush_keeps_only_latest_digest(conn, settings):
    sent = []
    closed = _wa(conn, settings, open_window=False, sent=sent)
    closed.send_text("digest one", kind="digest")
    closed.send_text("digest two", kind="digest")
    closed.send_text("interview!", kind="alert", priority=digest.PRIORITY_ALERT)
    assert sent == []
    result = _wa(conn, settings, sent=sent).flush()
    assert result == {"sent": 2, "failed": 0, "dropped": 1, "left": 0}
    assert "interview!" in sent[0] and "digest two" in sent[1]


def test_footer_mentions_problems(conn):
    conn.execute(
        "INSERT INTO source_health(source, consecutive_failures, disabled_until) VALUES ('linkedin', 3, ?)",
        ("2099-01-01T00:00:00.000Z",),
    )
    assert "paused after errors" in digest.health_footer(conn, NOW)
