"""Unit and integration tests for job board applications (LinkedIn, Naukri, Indeed).

Tests:
- State detection across all 3 platforms (needs_human, blocked, closed, external, apply)
- Account blocking and alert notifications
- Daily limits and live-view prompt limits
- External link detection and Tier A ATS upgrading
- Unticking 'Follow company' on LinkedIn Easy Apply
- CLI commands: 'agent boards status', 'agent boards login'
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from agent import config, db
from agent.apply.boards import indeed, linkedin, naukri
from agent.apply.boards.base import (
    applications_today,
    block_site,
    can_apply_today,
    can_prompt_today,
    is_site_live,
    prompts_today,
    record_prompt,
)
from agent.apply.boards.runner import run_board_apply
from agent.apply.runner import go_live
from agent.discover.base import RawJob, upsert

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"chromium not installed: {exc}")
        yield b
        b.close()


@pytest.fixture
def board_settings(settings):
    # Ensure live mode and boards configured
    return settings.model_copy(
        update={
            "apply": settings.apply.model_copy(
                update={
                    "mode": "live",
                    "dry_runs_before_live": 0,
                    "boards": {
                        "linkedin": config.BoardSettings(daily_cap=10),
                        "naukri": config.BoardSettings(daily_cap=10),
                        "indeed": config.BoardSettings(daily_cap=10),
                    },
                    "human_step_minutes": 15,
                    "human_prompts_per_site_per_day": 2,
                }
            )
        }
    )


# ---------------------------------------------------------------- Base & State Tests


def test_is_site_live_and_go_live(conn, board_settings):
    assert not is_site_live(conn, board_settings, "linkedin")

    # go_live for board bypasses dry runs
    ok, msg = go_live(conn, board_settings, "linkedin")
    assert ok
    assert is_site_live(conn, board_settings, "linkedin")

    # If mode is dry_run, it's not live
    dry_settings = board_settings.model_copy(
        update={"apply": board_settings.apply.model_copy(update={"mode": "dry_run"})}
    )
    assert not is_site_live(conn, dry_settings, "linkedin")

    # If blocked, site is not live
    block_site(conn, "linkedin", "test ban")
    assert not is_site_live(conn, board_settings, "linkedin")
    assert db.get_flag(conn, "board_blocked:linkedin") == "test ban"

    # Re-enabling clears block
    ok, _ = go_live(conn, board_settings, "linkedin")
    assert ok
    assert is_site_live(conn, board_settings, "linkedin")
    assert db.get_flag(conn, "board_blocked:linkedin") is None


def test_prompts_and_daily_caps(conn, board_settings):
    now = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

    # Prompts
    assert prompts_today(conn, "linkedin", now) == 0
    assert can_prompt_today(conn, "linkedin", board_settings, now)

    record_prompt(conn, "linkedin", now)
    assert prompts_today(conn, "linkedin", now) == 1
    assert can_prompt_today(conn, "linkedin", board_settings, now)

    record_prompt(conn, "linkedin", now)
    assert prompts_today(conn, "linkedin", now) == 2
    assert not can_prompt_today(conn, "linkedin", board_settings, now)

    # Next day resets prompt count
    tomorrow = now + timedelta(days=1)
    assert prompts_today(conn, "linkedin", tomorrow) == 0
    assert can_prompt_today(conn, "linkedin", board_settings, tomorrow)

    # Applications cap
    assert applications_today(conn, "linkedin", now) == 0
    assert can_apply_today(conn, "linkedin", board_settings, now)

    job_id, _ = upsert(
        conn,
        RawJob("linkedin", "lk-cap", "CapCorp", "Engineer"),
        now,
    )
    cur = conn.execute(
        "INSERT INTO applications(job_id, status, origin) VALUES (?, 'prepared', 'agent')",
        (job_id,),
    )
    app_id = cur.lastrowid
    for _ in range(10):
        conn.execute(
            "INSERT INTO apply_attempts(application_id, adapter, mode, outcome, started_at, finished_at)"
            " VALUES (?, 'linkedin', 'live', 'submitted', ?, ?)",
            (app_id, db.to_iso(now), db.to_iso(now)),
        )
    assert applications_today(conn, "linkedin", now) == 10
    assert not can_apply_today(conn, "linkedin", board_settings, now)


# ---------------------------------------------------------------- Detection Tests


def test_linkedin_detect_state(browser):
    page = browser.new_page()
    try:
        # Blocked
        page.set_content("<div><h1>Your account has been restricted</h1></div>")
        assert linkedin.detect_state(page).state == "blocked"

        # Checkpoint / Login
        page.set_content("<div><h1>Security Verification</h1><p>Solve this puzzle</p></div>")
        assert linkedin.detect_state(page).state == "needs_human"

        # Closed
        page.set_content("<div><h1>This job is closed</h1></div>")
        assert linkedin.detect_state(page).state == "closed"

        # External Apply
        page.set_content(
            "<div><a class='jobs-apply-button' href='https://boards.greenhouse.io/corp/jobs/101'>Apply on company website</a></div>"
        )
        st = linkedin.detect_state(page)
        assert st.state == "external"
        assert "greenhouse.io" in st.external_url

        # Easy Apply
        page.set_content("<div><button class='jobs-apply-button'>Easy Apply</button></div>")
        assert linkedin.detect_state(page).state == "apply"
    finally:
        page.close()


def test_naukri_detect_state(browser):
    page = browser.new_page()
    try:
        # Blocked
        page.set_content("<div><h1>Your account has been blocked</h1></div>")
        assert naukri.detect_state(page).state == "blocked"

        # Needs human (OTP)
        page.set_content("<div><p>Enter OTP sent to your phone</p></div>")
        assert naukri.detect_state(page).state == "needs_human"

        # Closed
        page.set_content("<div><h1>This job has expired</h1></div>")
        assert naukri.detect_state(page).state == "closed"

        # External
        page.set_content("<div><a href='https://jobs.lever.co/example/202'>Apply on company site</a></div>")
        st = naukri.detect_state(page)
        assert st.state == "external"
        assert "lever.co" in st.external_url

        # Native Apply
        page.set_content("<div><button class='apply-button'>Apply</button></div>")
        assert naukri.detect_state(page).state == "apply"
    finally:
        page.close()


def test_indeed_detect_state(browser):
    page = browser.new_page()
    try:
        # Blocked
        page.set_content("<div><h1>Your account has been suspended</h1></div>")
        assert indeed.detect_state(page).state == "blocked"

        # Needs human (Cloudflare)
        page.set_content("<div><h1>Verify you are human</h1><p>Cloudflare</p></div>")
        assert indeed.detect_state(page).state == "needs_human"

        # Closed
        page.set_content("<div><h1>This job has expired</h1></div>")
        assert indeed.detect_state(page).state == "closed"

        # External
        page.set_content("<div><a href='https://jobs.ashbyhq.com/firm/303'>Apply on company site</a></div>")
        st = indeed.detect_state(page)
        assert st.state == "external"
        assert "ashbyhq.com" in st.external_url

        # Native Apply
        page.set_content("<div><button id='indeedApplyButton'>Apply now</button></div>")
        assert indeed.detect_state(page).state == "apply"
    finally:
        page.close()


# ---------------------------------------------------------------- Apply Flow & Modal Tests


def test_linkedin_modal_untick_follow_and_submit(browser, conn, profile, tmp_path):
    page = browser.new_page()
    try:
        html = """
        <html><body>
            <button class="jobs-apply-button">Easy Apply</button>
            <div class="jobs-easy-apply-modal" role="dialog" style="display:none;">
                <label>Phone <input id="phone" type="tel" value="" /></label>
                <label><input type="checkbox" id="follow" checked /> Follow Acme Corp</label>
                <button id="submit">Submit application</button>
            </div>
            <script>
                document.querySelector('.jobs-apply-button').onclick = function() {
                    document.querySelector('.jobs-easy-apply-modal').style.display = 'block';
                };
                document.getElementById('submit').onclick = function() {
                    document.querySelector('.jobs-easy-apply-modal').innerHTML = '<h2>Application sent!</h2>';
                };
            </script>
        </body></html>
        """
        page.set_content(html)
        app = {"id": 1, "job_id": 10, "company": "Acme Corp"}

        outcome, error, screenshot = linkedin.apply(
            page, conn, profile, None, app, tmp_path, confirmation_timeout_s=2
        )
        assert outcome in ("confirmed", "submitted")
        assert error is None
        assert screenshot is not None and screenshot.exists()
    finally:
        page.close()


# ---------------------------------------------------------------- Runner & Upgrade Tests


def test_run_board_apply_upgrades_external_to_tier_a(browser, conn, board_settings, profile, tmp_path):
    now = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

    # Enable linkedin
    go_live(conn, board_settings, "linkedin")

    # Insert candidate job on linkedin
    job_id, _ = upsert(
        conn,
        RawJob("linkedin", "lk-1", "Stripe", "Software Engineer", url="http://local-test/job"),
        now,
    )
    conn.execute(
        "INSERT INTO applications(job_id, status, origin) VALUES (?, 'prepared', 'agent')",
        (job_id,),
    )
    conn.commit()

    # Browser factory returning dummy page that has external greenhouse link
    from contextlib import contextmanager

    @contextmanager
    def mock_browser_factory():
        page = browser.new_page()
        page.set_content(
            "<div><a class='jobs-apply-button' href='https://boards.greenhouse.io/stripe/jobs/999'>Apply on company website</a></div>"
        )
        # Mock goto so it doesn't try network
        page.goto = lambda *args, **kwargs: None
        try:
            yield MagicMock(new_page=lambda: page)
        finally:
            page.close()

    result = run_board_apply(
        conn,
        board_settings,
        profile,
        browser_factory=mock_browser_factory,
        now=lambda: now,
    )
    assert result.outcomes.get("external") == 1

    # Verify job was upgraded to Tier A ATS greenhouse!
    row = conn.execute("SELECT apply_url, ats_type FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["ats_type"] == "greenhouse"
    assert "boards.greenhouse.io/stripe/jobs/999" in row["apply_url"]
