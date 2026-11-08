"""Board apply stage: handles opt-in live applications on LinkedIn, Naukri, and Indeed.

Runs headed on Xvfb :99 with a dedicated persistent browser profile in /data/job-agent/browser-boards/.
Respects daily caps (10/day/site) and volume limits (1 application/site/cycle).
Routes external ATS links (Greenhouse, Lever, Ashby) to Tier A.
Invokes on-demand noVNC live view when login/OTP/captcha challenges arise (max 2 prompts/site/day).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agent import db, killswitch, paths
from agent.apply.boards import indeed, linkedin, naukri
from agent.apply.boards.base import (
    PageState,
    block_site,
    can_apply_today,
    can_prompt_today,
    is_site_live,
    record_prompt,
)
from agent.config import Profile, Settings
from agent.discover.base import detect_ats
from agent.live_view import (
    is_display_active,
    live_view,
    start_xvfb,
    stop_xvfb,
    wait_for_resolution,
)
from agent.runs import stage_run

log = logging.getLogger(__name__)


@dataclass
class BoardApplyResult:
    attempted: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    submitted: list[str] = field(default_factory=list)
    stopped: str | None = None


def _get_site_adapter(site: str):
    if site == "linkedin":
        return linkedin
    if site == "naukri":
        return naukri
    if site == "indeed":
        return indeed
    return None


@contextmanager
def default_board_browser(settings: Settings, display: str = ":99"):
    from playwright.sync_api import sync_playwright

    xvfb_proc = None
    if not is_display_active(display) and os.name != "nt":
        xvfb_proc = start_xvfb(display)

    profile_dir = paths.browser_boards_dir()
    os.environ["DISPLAY"] = display

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False if os.name != "nt" else settings.apply.headless,
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-http2"]
            if os.name != "nt"
            else ["--disable-http2"],
        )
        try:
            yield context
        finally:
            context.close()
            if xvfb_proc:
                stop_xvfb(xvfb_proc, display)


def run_board_apply(
    conn: sqlite3.Connection,
    settings: Settings,
    profile: Profile,
    client=None,
    cloud=None,
    browser_factory: Callable | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    wa=None,
) -> BoardApplyResult:
    result = BoardApplyResult()
    if settings.apply.mode != "live":
        result.stopped = "apply.mode is not live"
        return result

    with stage_run(conn, "board-apply") as run:
        if killswitch.is_stopped(conn, cloud):
            result.stopped = "kill switch"
            return result

        current_now = now()
        active_sites = [
            site
            for site in ("linkedin", "naukri", "indeed")
            if is_site_live(conn, settings, site) and can_apply_today(conn, site, settings, current_now)
        ]
        if not active_sites:
            log.info("no active board sites due for application this cycle")
            return result

        factory = browser_factory or (lambda: default_board_browser(settings))
        with factory() as context:
            for site in active_sites:
                adapter = _get_site_adapter(site)
                if not adapter:
                    continue

                candidates_evaluated = 0
                max_evaluations = 5

                while candidates_evaluated < max_evaluations:
                    if killswitch.is_stopped(conn, cloud):
                        result.stopped = "kill switch"
                        break

                    # Pick top 1 candidate application for this board site
                    candidate = conn.execute(
                        "SELECT a.*, j.id as job_id, j.title, j.company, j.url, j.apply_url, j.ats_type, j.source"
                        " FROM applications a JOIN jobs j ON j.id = a.job_id"
                        " WHERE a.status = 'prepared' AND a.origin = 'agent' AND j.source = ?"
                        " ORDER BY a.id LIMIT 1",
                        (site,),
                    ).fetchone()

                    if not candidate:
                        break

                    candidates_evaluated += 1
                    target_url = candidate["apply_url"] or candidate["url"]
                    if not target_url:
                        conn.execute(
                            "UPDATE applications SET status = 'skipped', skip_reason = 'missing url' WHERE id = ?",
                            (candidate["id"],),
                        )
                        continue

                    app_dir = paths.application_dir(candidate["id"])
                    page = context.new_page()
                    try:
                        page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)

                        state: PageState = adapter.detect_state(page)

                        # 1. Blocked / restricted
                        if state.state == "blocked":
                            block_site(conn, site, state.detail or "restricted", wa)
                            conn.execute(
                                "UPDATE applications SET status = 'skipped', skip_reason = ? WHERE id = ?",
                                (f"blocked: {state.detail}", candidate["id"]),
                            )
                            result.outcomes["blocked"] = result.outcomes.get("blocked", 0) + 1
                            break

                        # 2. Closed
                        if state.state == "closed":
                            conn.execute(
                                "UPDATE applications SET status = 'skipped', skip_reason = ? WHERE id = ?",
                                (f"closed: {state.detail}", candidate["id"]),
                            )
                            result.outcomes["closed"] = result.outcomes.get("closed", 0) + 1
                            continue

                        # 3. External apply
                        if state.state == "external":
                            ext_url = state.external_url or target_url
                            detected = detect_ats(ext_url)
                            if detected and detected[0] in settings.apply.adapters:
                                # Upgrade job to Tier A ATS
                                conn.execute(
                                    "UPDATE jobs SET apply_url = ?, ats_type = ? WHERE id = ?",
                                    (ext_url, detected[0], candidate["job_id"]),
                                )
                                log.info(
                                    "Job %d upgraded to Tier A ATS (%s): %s",
                                    candidate["job_id"],
                                    detected[0],
                                    ext_url,
                                )
                            else:
                                conn.execute(
                                    "UPDATE applications SET status = 'skipped', skip_reason = ? WHERE id = ?",
                                    (f"external: {ext_url}", candidate["id"]),
                                )
                            result.outcomes["external"] = result.outcomes.get("external", 0) + 1
                            continue

                        # 4. Needs human (login, OTP, captcha)
                        if state.state == "needs_human":
                            if not can_prompt_today(conn, site, settings, current_now):
                                log.warning(
                                    "Daily human prompt limit reached for %s; skipping for today", site
                                )
                                break

                            record_prompt(conn, site, current_now)
                            with live_view(
                                site,
                                state.detail or "verification",
                                wa_client=wa,
                                timeout_minutes=settings.apply.human_step_minutes,
                            ) as session:
                                if session is None:
                                    log.warning("could not open live view session for %s", site)
                                    break

                                def check_resolved(cur_adapter=adapter, cur_page=page):
                                    st = cur_adapter.detect_state(cur_page)
                                    return st.state not in ("needs_human", "blocked")

                                resolved = wait_for_resolution(
                                    session,
                                    check_resolved,
                                    timeout_seconds=settings.apply.human_step_minutes * 60,
                                )
                                if not resolved:
                                    log.warning("human resolution timed out for %s", site)
                                    break

                            state = adapter.detect_state(page)
                            if state.state != "apply":
                                break

                        # 5. Apply
                        if state.state == "apply":
                            started_dt = now()
                            outcome, error, screenshot = adapter.apply(
                                page,
                                conn,
                                profile,
                                client,
                                candidate,
                                app_dir,
                                settings.apply.confirmation_timeout_s,
                            )
                            finished_dt = now()

                            # Record to apply_attempts
                            conn.execute(
                                "INSERT INTO apply_attempts(application_id, adapter, mode, outcome, error, screenshot_path, started_at, finished_at)"
                                " VALUES (?, ?, 'live', ?, ?, ?, ?, ?)",
                                (
                                    candidate["id"],
                                    site,
                                    outcome,
                                    error,
                                    str(screenshot) if screenshot else None,
                                    db.to_iso(started_dt),
                                    db.to_iso(finished_dt),
                                ),
                            )

                            if outcome in ("submitted", "confirmed"):
                                conn.execute(
                                    "UPDATE applications SET status = 'submitted', submitted_at = ?, confirmed_at = ?, follow_up_due = ?"
                                    " WHERE id = ?",
                                    (
                                        db.to_iso(finished_dt),
                                        db.to_iso(finished_dt) if outcome == "confirmed" else None,
                                        db.to_iso(finished_dt + timedelta(days=7)),
                                        candidate["id"],
                                    ),
                                )
                                result.submitted.append(
                                    f"{candidate['title']} — {candidate['company']} ({site.capitalize()})"
                                )
                            else:
                                conn.execute(
                                    "UPDATE applications SET status = 'skipped', skip_reason = ? WHERE id = ?",
                                    (f"{outcome}: {error or ''}", candidate["id"]),
                                )

                            result.attempted += 1
                            result.outcomes[outcome] = result.outcomes.get(outcome, 0) + 1
                            break

                    except Exception as exc:
                        log.error("error applying on %s for job %d: %s", site, candidate["job_id"], exc)
                        result.outcomes["failed"] = result.outcomes.get("failed", 0) + 1
                        break
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass

        run.items = result.attempted
    return result
