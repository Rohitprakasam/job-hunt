"""Apply stage. One headless browser, one application at a time.

Order per application (any failure = skip and log, never a half-right submit):
kill switch -> volume gate -> open form -> block gate -> plan fields -> answer gate -> fill ->
read back (form gate) -> screenshot + saved values (proof gate) -> [live only] kill switch again ->
submit -> confirmation.

Modes: dry_run fills and verifies but never clicks submit. live submits, and only for adapters that
have passed `dry_runs_before_live` clean dry runs and were switched on with `agent apply go-live`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent import db, killswitch, paths
from agent.apply import form
from agent.apply.adapters import ADAPTERS, Adapter
from agent.config import Profile, Settings
from agent.gates import GateResult, answer_gate, as_json, block_gate, form_gate, proof_gate, volume_gate
from agent.runs import stage_run

log = logging.getLogger(__name__)

LIVE_FLAG = "live_enabled:{adapter}"


@dataclass
class AttemptResult:
    outcome: str  # filled | submitted | confirmed | blocked | failed | skipped
    gates: list[GateResult] = field(default_factory=list)
    error: str | None = None
    screenshot: Path | None = None


def clean_dry_runs(conn: sqlite3.Connection, adapter: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM apply_attempts WHERE adapter = ? AND mode = 'dry_run' AND outcome = 'filled'",
        (adapter,),
    ).fetchone()[0]


def live_allowed(conn: sqlite3.Connection, settings: Settings, adapter: str) -> bool:
    return settings.apply.mode == "live" and db.get_flag(conn, LIVE_FLAG.format(adapter=adapter)) == "1"


def go_live(conn: sqlite3.Connection, settings: Settings, adapter: str) -> tuple[bool, str]:
    done = clean_dry_runs(conn, adapter)
    need = settings.apply.dry_runs_before_live
    if done < need:
        return False, f"{adapter}: {done}/{need} clean dry runs; run more dry runs first"
    db.set_flag(conn, LIVE_FLAG.format(adapter=adapter), "1")
    return True, f"{adapter}: live submissions enabled (apply.mode must also be 'live' in settings.yaml)"


def apply_go_dry(conn: sqlite3.Connection, adapter: str) -> None:
    db.set_flag(conn, LIVE_FLAG.format(adapter=adapter), "0")


def _settle(page, timeout_ms: int = 15000) -> None:
    """Single-page apps (Ashby) render the form after load: wait for fields, then for the network."""
    try:
        page.wait_for_selector("input:not([type=hidden]), textarea, select", timeout=timeout_ms)
    except Exception:
        pass  # no fields at all: the block gate / "no form" check decides
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def _page_text(page) -> str:
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:
        return ""


def _challenge_frames(page) -> list[str]:
    urls = []
    for frame in page.frames:
        url = frame.url or ""
        if ("captcha" in url or "challenges.cloudflare" in url) and "size=invisible" not in url:
            urls.append(url.split("?")[0])
    return urls


def attempt(
    page,
    adapter: Adapter,
    url: str,
    conn: sqlite3.Connection,
    profile: Profile,
    client,
    app: sqlite3.Row,
    out_dir: Path,
    live: bool,
    stopped: Callable[[], bool],
    confirmation_timeout_s: int = 30,
) -> AttemptResult:
    gates: list[GateResult] = []
    page.goto(adapter.form_url(url), wait_until="domcontentloaded", timeout=45000)
    _settle(page)
    if adapter.open_form_selector and not page.locator("input[type=file]").count():
        opener = page.locator(adapter.open_form_selector).first
        if opener.count():
            opener.click()
            page.wait_for_timeout(1500)

    block = block_gate(_page_text(page), _challenge_frames(page), unknown_steps=False)
    gates.append(block)
    if not block.ok:
        outcome = (
            "captcha"
            if ("captcha" in block.detail.lower() or "challenge" in block.detail.lower())
            else "blocked"
        )
        return AttemptResult(outcome, gates, block.detail)

    fields = [form.Field.from_js(raw) for raw in page.evaluate(form.EXTRACT_JS)]
    if not any(f.kind == "file" for f in fields) and not any("email" in f.label.lower() for f in fields):
        gates.append(GateResult("block", False, "no application form found (unknown flow)"))
        return AttemptResult("blocked", gates, "no form")
    resume = Path(app["resume_path"]) if app["resume_path"] else None
    cover = Path(app["cover_path"]).read_text(encoding="utf-8") if app["cover_path"] else ""
    company = app["company"] if "company" in app.keys() else None
    planned = form.plan_fields(fields, conn, profile, client, resume, cover, app["job_id"], company)
    answer = answer_gate([p.plan for p in planned])
    gates.append(answer)
    if not answer.ok:
        return AttemptResult("skipped", gates, answer.detail)

    try:
        form.fill(page, planned)
    except Exception as exc:
        gates.append(GateResult("form", False, f"fill error: {type(exc).__name__}"))
        return AttemptResult("failed", gates, str(exc)[:200])
    page.wait_for_timeout(500)
    errors = [e for e in page.evaluate(form.ERRORS_JS) if e]
    check = form_gate(form.intended(planned), form.read_back(page, planned), errors)
    gates.append(check)
    if not check.ok:
        return AttemptResult("failed", gates, check.detail)

    shot = out_dir / ("filled-live.png" if live else "filled-dry-run.png")
    page.screenshot(path=str(shot), full_page=True)
    values = out_dir / "submitted-values.json"
    sent = {p.field.label: p.plan.value for p in planned if p.plan.value}
    values.write_text(
        json.dumps({"url": url, "mode": "live" if live else "dry_run", "values": sent}, indent=2),
        encoding="utf-8",
    )
    proof = proof_gate(shot, values.exists())
    gates.append(proof)
    if not proof.ok:
        return AttemptResult("failed", gates, proof.detail, shot)

    if not live:
        return AttemptResult("filled", gates, None, shot)

    if stopped():  # kill switch checked again right before the click
        gates.append(GateResult("killswitch", False, "stop requested"))
        return AttemptResult("skipped", gates, "stop requested", shot)
    page.locator(adapter.submit_selector).first.click()
    try:
        page.wait_for_function(
            "re => new RegExp(re, 'i').test(document.body.innerText)",
            arg=adapter.confirmation.pattern,
            timeout=confirmation_timeout_s * 1000,
        )
        outcome = "confirmed"
    except Exception:
        errors = [e for e in page.evaluate(form.ERRORS_JS) if e]
        if _challenge_frames(page) or any("captcha" in str(e).lower() for e in errors):
            outcome = "captcha"
        else:
            outcome = "failed" if errors else "submitted"
    page.screenshot(path=str(out_dir / "after-submit.png"), full_page=True)
    return AttemptResult(outcome, gates, None, shot)


def _record(conn, app_id, adapter, live, result: AttemptResult, started: datetime, now: datetime) -> None:
    conn.execute(
        "INSERT INTO apply_attempts(application_id, adapter, mode, outcome, error, screenshot_path, started_at,"
        " finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            app_id, adapter, "live" if live else "dry_run", result.outcome, result.error,
            str(result.screenshot) if result.screenshot else None, db.to_iso(started), db.to_iso(now),
        ),
    )  # fmt: skip
    previous = json.loads(
        conn.execute("SELECT gate_results_json FROM applications WHERE id = ?", (app_id,)).fetchone()[0]
        or "[]"
    )
    gates = previous + as_json(result.gates)
    if live and result.outcome in ("submitted", "confirmed"):
        conn.execute(
            "UPDATE applications SET status = 'submitted', submitted_at = ?, confirmed_at = ?, gate_results_json = ?,"
            " follow_up_due = ? WHERE id = ?",
            (
                db.to_iso(now), db.to_iso(now) if result.outcome == "confirmed" else None, json.dumps(gates),
                db.to_iso(now + timedelta(days=7)), app_id,
            ),
        )  # fmt: skip
    elif result.outcome in ("blocked", "skipped", "captcha") or (live and result.outcome == "failed"):
        conn.execute(
            "UPDATE applications SET status = 'skipped', skip_reason = ?, gate_results_json = ? WHERE id = ?",
            (f"{result.outcome}: {result.error or ''}"[:300], json.dumps(gates), app_id),
        )
    else:
        conn.execute(
            "UPDATE applications SET gate_results_json = ? WHERE id = ?", (json.dumps(gates), app_id)
        )


@dataclass
class ApplyResult:
    attempted: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    stopped: str | None = None
    submitted: list[str] = field(default_factory=list)  # "title — company" for the WhatsApp summary


def run_apply(
    conn: sqlite3.Connection,
    settings: Settings,
    profile: Profile,
    client=None,
    cloud=None,
    limit: int = 5,
    browser_factory: Callable | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    wa=None,
) -> ApplyResult:
    result = ApplyResult()
    if settings.apply.mode == "off":
        result.stopped = "apply.mode is off"
        return result
    with stage_run(conn, "apply") as run:
        if killswitch.is_stopped(conn, cloud):
            result.stopped = "kill switch"
            return result
        # Each prepared application gets one dry run; once its adapter is live, it gets the real submit.
        rows = conn.execute(
            "SELECT a.*, j.apply_url, j.url, j.ats_type, j.company, j.title,"
            " (SELECT count(*) FROM apply_attempts t WHERE t.application_id = a.id AND t.mode = 'dry_run'"
            "  AND t.outcome != 'failed') AS dry_runs FROM applications a JOIN jobs j ON j.id = a.job_id"
            " WHERE a.status = 'prepared' AND a.origin = 'agent'"
            " AND NOT EXISTS (SELECT 1 FROM apply_attempts t WHERE t.application_id = a.id AND t.mode = 'live')"
            " ORDER BY a.id",
        ).fetchall()
        apps = [r for r in rows if live_allowed(conn, settings, r["ats_type"] or "") or not r["dry_runs"]][
            :limit
        ]
        if not apps:
            return result
        factory = browser_factory or _default_browser(settings)
        with factory() as context:
            for app in apps:
                adapter = ADAPTERS.get(app["ats_type"])
                if adapter is None or adapter.name not in settings.apply.adapters:
                    continue
                live = live_allowed(conn, settings, adapter.name)
                if live:
                    volume = volume_gate(conn, app["company"], settings.apply, now(), adapter=adapter.name)
                    if not volume.ok:
                        if "failed in a row" in volume.detail:
                            apply_go_dry(conn, adapter.name)
                            msg = f"{adapter.name.capitalize()} paused after {settings.apply.max_consecutive_failures} failed submits; its jobs go to your list"
                            log.warning(msg)
                            try:
                                from agent.notify.whatsapp import WhatsApp

                                wa_client = wa or WhatsApp(settings.whatsapp, conn, cloud)
                                wa_client.send_text(msg, priority=8)
                            except Exception as alert_exc:
                                log.warning("failed to send WhatsApp pause alert: %s", alert_exc)
                            live = False
                        else:
                            result.stopped = volume.detail
                            break
                started = now()
                page = context.new_page()
                try:
                    attempt_result = attempt(
                        page, adapter, app["apply_url"] or app["url"], conn, profile, client, app,
                        paths.application_dir(app["id"]), live, lambda: killswitch.is_stopped(conn, cloud),
                        settings.apply.confirmation_timeout_s,
                    )  # fmt: skip
                except Exception as exc:  # timeouts, navigation errors: one failed attempt, keep going
                    attempt_result = AttemptResult("failed", [], f"{type(exc).__name__}: {str(exc)[:150]}")
                finally:
                    page.close()
                _record(conn, app["id"], adapter.name, live, attempt_result, started, now())
                result.attempted += 1
                result.outcomes[attempt_result.outcome] = result.outcomes.get(attempt_result.outcome, 0) + 1
                if live and attempt_result.outcome in ("submitted", "confirmed"):
                    result.submitted.append(f"{app['title']} — {app['company']}")
        run.items = result.attempted
    return result


def _default_browser(settings: Settings):
    from contextlib import contextmanager

    from playwright.sync_api import sync_playwright

    @contextmanager
    def factory():
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(paths.browser_profile_dir()), headless=settings.apply.headless,
                viewport={"width": 1280, "height": 1800}, locale="en-IN",
            )  # fmt: skip
            try:
                yield context
            finally:
                context.close()

    return factory
