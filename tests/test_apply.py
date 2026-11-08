"""Apply stage against saved form fixtures in a real headless Chromium (skipped if not installed)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent import db
from agent.apply import runner
from agent.apply.adapters import GENERIC
from agent.discover.base import RawJob, upsert
from agent.gates import consecutive_failures, volume_gate
from agent.prepare import answers
from tests.test_prepare import NOW

FORMS = Path(__file__).parent / "fixtures" / "forms"

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:  # browser not downloaded
            pytest.skip(f"chromium not installed: {exc}")
        yield b
        b.close()


@pytest.fixture
def app_row(conn, tmp_path, profile):
    answers.seed(conn, profile)
    job_id, _ = upsert(
        conn, RawJob("greenhouse", "1", "Acme", "Security Analyst", ats_type="greenhouse"), NOW
    )
    resume = tmp_path / "Testa_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    cover = tmp_path / "cover.txt"
    cover.write_text("I build reliable Linux systems.", encoding="utf-8")
    cur = conn.execute(
        "INSERT INTO applications(job_id, status, origin, resume_path, cover_path) VALUES (?, 'prepared', 'agent', ?, ?)",
        (job_id, str(resume), str(cover)),
    )
    return conn.execute("SELECT * FROM applications WHERE id = ?", (cur.lastrowid,)).fetchone()


def _attempt(browser, conn, profile, app_row, tmp_path, name, live=False, stopped=lambda: False):
    page = browser.new_page()
    try:
        result = runner.attempt(
            page, GENERIC, (FORMS / name).as_uri(), conn, profile, None, app_row, tmp_path, live, stopped, 5
        )
        submitted = page.evaluate("() => window.__submitted === true")
    finally:
        page.close()
    return result, submitted


def test_dry_run_fills_verifies_and_never_submits(browser, conn, profile, app_row, tmp_path):
    result, submitted = _attempt(browser, conn, profile, app_row, tmp_path, "basic.html")
    assert result.outcome == "filled", [vars(g) for g in result.gates]
    assert submitted is False
    assert {g.gate for g in result.gates} >= {"block", "answers", "form", "proof"}
    values = json.loads((tmp_path / "submitted-values.json").read_text(encoding="utf-8"))["values"]
    assert values["First Name"] == "Testa" and values["Resume/CV"] == "Testa_Resume.pdf"
    assert values["Are you legally authorized to work in India?"] == "Yes"
    assert values["Will you now or in the future require sponsorship for employment visa status?"] == "No"
    assert values["Cover Letter"].startswith("I build")
    assert "Anything else you'd like to share?" not in values  # optional unknown: left blank, not guessed
    assert (tmp_path / "filled-dry-run.png").stat().st_size > 0


def test_unanswerable_required_question_skips(browser, conn, profile, app_row, tmp_path):
    result, submitted = _attempt(browser, conn, profile, app_row, tmp_path, "unanswerable.html")
    assert result.outcome == "skipped" and "favourite colour" in result.error and not submitted
    assert conn.execute("SELECT count(*) FROM pending_questions").fetchone()[0] == 1


def test_captcha_blocks(browser, conn, profile, app_row, tmp_path):
    result, _ = _attempt(browser, conn, profile, app_row, tmp_path, "captcha.html")
    assert result.outcome == "captcha"


def test_live_submits_and_confirms(browser, conn, profile, app_row, tmp_path):
    result, submitted = _attempt(browser, conn, profile, app_row, tmp_path, "basic.html", live=True)
    assert result.outcome == "confirmed" and submitted is True


def test_kill_switch_checked_right_before_submit(browser, conn, profile, app_row, tmp_path):
    result, submitted = _attempt(
        browser, conn, profile, app_row, tmp_path, "basic.html", live=True, stopped=lambda: True
    )
    assert result.outcome == "skipped" and submitted is False


def test_go_live_needs_clean_dry_runs(conn, settings, app_row):
    strict = settings.model_copy(
        update={"apply": settings.apply.model_copy(update={"dry_runs_before_live": 5, "mode": "dry_run"})}
    )
    ok, message = runner.go_live(conn, strict, "greenhouse")
    assert not ok and "0/5" in message
    for _ in range(5):
        conn.execute(
            "INSERT INTO apply_attempts(application_id, adapter, mode, outcome, started_at)"
            " VALUES (?, 'greenhouse', 'dry_run', 'filled', ?)",
            (app_row["id"], db.utcnow()),
        )
    assert runner.go_live(conn, strict, "greenhouse")[0]
    assert not runner.live_allowed(conn, strict, "greenhouse")  # mode is still dry_run
    live = strict.model_copy(update={"apply": strict.apply.model_copy(update={"mode": "live"})})
    assert runner.live_allowed(conn, live, "greenhouse")

    # When dry_runs_before_live is 0, live can be enabled immediately with 0 dry runs
    zero_runs = settings.model_copy(
        update={"apply": settings.apply.model_copy(update={"dry_runs_before_live": 0})}
    )
    ok_zero, _ = runner.go_live(conn, zero_runs, "lever")
    assert ok_zero


def test_greenhouse_like_widgets(browser, conn, profile, app_row, tmp_path):
    """'Attach' upload buttons, custom dropdowns with hidden twin inputs, profile rules."""
    conn.execute("UPDATE jobs SET company = 'Acme'")
    app = conn.execute(
        "SELECT a.*, j.company FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?",
        (app_row["id"],),
    ).fetchone()
    result, submitted = _attempt(browser, conn, profile, app, tmp_path, "greenhouse_like.html")
    assert result.outcome == "filled", [vars(g) for g in result.gates]
    values = json.loads((tmp_path / "submitted-values.json").read_text(encoding="utf-8"))["values"]
    assert values["Resume/CV"] == "Testa_Resume.pdf"
    assert (
        values[
            "Will you now or in the future require sponsorship for a visa to remain in your current country?"
        ]
        == "No"
    )
    assert values["Have you previously worked at or consulted for Acme?"] == "No"
    assert len(values) == 5 and not submitted


def test_per_ats_failure_pausing(conn, settings, app_row):
    live_settings = settings.model_copy(
        update={
            "apply": settings.apply.model_copy(
                update={"dry_runs_before_live": 0, "mode": "live", "max_consecutive_failures": 3}
            )
        }
    )
    runner.go_live(conn, live_settings, "greenhouse")
    runner.go_live(conn, live_settings, "lever")
    assert runner.live_allowed(conn, live_settings, "greenhouse")
    assert runner.live_allowed(conn, live_settings, "lever")

    # Greenhouse gets 3 live failures
    now = datetime.now(UTC) + timedelta(seconds=1)
    for _ in range(3):
        conn.execute(
            "INSERT INTO apply_attempts(application_id, adapter, mode, outcome, started_at)"
            " VALUES (?, 'greenhouse', 'live', 'failed', ?)",
            (app_row["id"], db.to_iso(now)),
        )

    # Volume gate for greenhouse trips
    gate_gh = volume_gate(conn, "Acme", live_settings.apply, now, adapter="greenhouse")
    assert not gate_gh.ok
    assert "3 submissions failed in a row for greenhouse" in gate_gh.detail

    # Volume gate for lever is unaffected
    gate_lever = volume_gate(conn, "Acme", live_settings.apply, now, adapter="lever")
    assert gate_lever.ok

    # Pausing greenhouse with apply_go_dry keeps lever live
    runner.apply_go_dry(conn, "greenhouse")
    assert not runner.live_allowed(conn, live_settings, "greenhouse")
    assert runner.live_allowed(conn, live_settings, "lever")


def test_consecutive_failures_resets_on_go_live(conn, settings, app_row):
    live_settings = settings.model_copy(
        update={
            "apply": settings.apply.model_copy(
                update={"dry_runs_before_live": 0, "mode": "live", "max_consecutive_failures": 3}
            )
        }
    )
    # Live was previously enabled 1 hour ago
    db.set_flag(conn, "live_enabled_at:greenhouse", db.to_iso(datetime.now(UTC) - timedelta(hours=1)))
    db.set_flag(conn, "live_enabled:greenhouse", "1")
    past = datetime.now(UTC) - timedelta(minutes=10)
    # 3 failures trip the consecutive failures count
    for i in range(3):
        conn.execute(
            "INSERT INTO apply_attempts(application_id, adapter, mode, outcome, started_at)"
            " VALUES (?, 'greenhouse', 'live', 'failed', ?)",
            (app_row["id"], db.to_iso(past + timedelta(seconds=i))),
        )
    # Before reset, all 3 are counted
    assert consecutive_failures(conn, "greenhouse") == 3

    # Re-enabling live via go_live resets the starting time so previous failures don't count
    runner.go_live(conn, live_settings, "greenhouse")
    assert consecutive_failures(conn, "greenhouse") == 0

    # 1 new failure counts as 1, not 4
    conn.execute(
        "INSERT INTO apply_attempts(application_id, adapter, mode, outcome, started_at)"
        " VALUES (?, 'greenhouse', 'live', 'failed', ?)",
        (app_row["id"], db.to_iso(datetime.now(UTC) + timedelta(seconds=1))),
    )
    assert consecutive_failures(conn, "greenhouse") == 1
    assert volume_gate(
        conn, "Acme", live_settings.apply, datetime.now(UTC) + timedelta(seconds=2), adapter="greenhouse"
    ).ok
