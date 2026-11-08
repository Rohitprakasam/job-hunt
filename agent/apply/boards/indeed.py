"""Indeed Apply adapter.

Handles:
- Logged-in / Cloudflare / hCaptcha detection -> needs_human
- Account restriction -> blocked
- Apply on company site -> external
- Indeed Apply multi-step flow: contact, resume, employer questions, review, submit
- Proof gate screenshot and confirmation text
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from agent.apply import form
from agent.apply.boards.base import PageState
from agent.config import Profile
from agent.prepare import answers

log = logging.getLogger(__name__)

INDEED_APPLY_SELECTORS = [
    "button#indeedApplyButton",
    "button:has-text('Apply now')",
    "button:has-text('Indeed Apply')",
    "a:has-text('Apply now')",
]

EXTERNAL_APPLY_SELECTORS = [
    "a:has-text('Apply on company site')",
    "button:has-text('Apply on company site')",
    "a[aria-label*='Apply on company site']",
]


def detect_state(page) -> PageState:
    url = page.url.lower()
    page_text = page.inner_text("body").lower()

    # 1. Blocked / restricted
    if (
        "account has been suspended" in page_text
        or "account has been restricted" in page_text
        or "access to indeed is restricted" in page_text
    ):
        return PageState("blocked", detail="Indeed account restriction detected")

    # 2. Needs human (Cloudflare, hCaptcha, login)
    if (
        "secure.indeed.com/auth" in url
        or "/account/login" in url
        or "verify you are human" in page_text
        or "cloudflare" in page_text
        or "cf-turnstile" in page_text
        or "hcaptcha" in page_text
        or "challenges.cloudflare.com" in url
    ):
        return PageState("needs_human", detail="Indeed Cloudflare challenge or login")

    # 3. Closed / already applied
    if "this job has expired" in page_text or "job is no longer available" in page_text:
        return PageState("closed", detail="Job has expired")
    if "you have already applied" in page_text or "applied on indeed" in page_text:
        return PageState("closed", detail="Already applied")

    # 4. External apply
    for sel in EXTERNAL_APPLY_SELECTORS:
        try:
            elem = page.query_selector(sel)
            if elem and elem.is_visible():
                href = elem.get_attribute("href")
                return PageState("external", detail="Apply on company site", external_url=href)
        except Exception:
            pass

    # 5. Native Indeed Apply
    for sel in INDEED_APPLY_SELECTORS:
        try:
            elem = page.query_selector(sel)
            if elem and elem.is_visible():
                return PageState("apply")
        except Exception:
            pass

    return PageState("closed", detail="No active Indeed Apply button found")


def apply(
    page,
    conn: sqlite3.Connection,
    profile: Profile,
    client,
    app,
    application_dir: Path,
    confirmation_timeout_s: int = 30,
) -> tuple[str, str | None, Path | None]:
    """Execute Indeed Apply multi-step flow. Returns (outcome, error, screenshot_path)."""
    clicked = False
    for sel in INDEED_APPLY_SELECTORS:
        try:
            elem = page.query_selector(sel)
            if elem and elem.is_visible():
                elem.click()
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        return "failed", "could not click Indeed Apply button", None

    screenshot_path = application_dir / "confirmation-indeed.png"
    max_steps = 10
    step = 0

    while step < max_steps:
        step += 1
        page.wait_for_timeout(1500)
        body = page.inner_text("body").lower()

        # Check confirmation
        if (
            "your application has been submitted" in body
            or "application submitted" in body
            or "submitted your application" in body
        ):
            page.screenshot(path=str(screenshot_path))
            return "confirmed", None, screenshot_path

        # 1. Resume upload if an input[type=file] is visible
        try:
            fi = page.query_selector("input[type='file']")
            if fi and fi.is_visible():
                resume_path = app["resume_path"] if "resume_path" in app.keys() else None
                if resume_path and os.path.exists(resume_path):
                    fi.set_input_files(str(resume_path))
                    page.wait_for_timeout(1000)
        except Exception as exc:
            log.warning("error attaching resume on Indeed: %s", exc)

        # 2. Form fields in current step
        try:
            fields = form.find_fields(page)
            if fields:
                planned = form.plan_fields(
                    fields, conn, profile, client, None, None, app["job_id"], app.get("company", "")
                )
                for f in planned:
                    if f.field.required and (f.value is None or str(f.value).strip() == ""):
                        answers.record_pending(conn, f.field.label, None)
                        return "skipped", f"unanswerable required question: {f.field.label}", None
                form.fill_form(page, planned)
        except Exception as exc:
            log.warning("error filling Indeed step: %s", exc)

        # 3. Check for Submit button
        submit_btn = None
        for submit_sel in [
            "button:has-text('Submit your application')",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
        ]:
            try:
                b = page.query_selector(submit_sel)
                if b and b.is_visible() and b.is_enabled():
                    submit_btn = b
                    break
            except Exception:
                pass

        if submit_btn:
            submit_btn.click()
            for _ in range(int(confirmation_timeout_s * 2)):
                page.wait_for_timeout(500)
                body = page.inner_text("body").lower()
                if "application has been submitted" in body or "application submitted" in body:
                    page.screenshot(path=str(screenshot_path))
                    return "confirmed", None, screenshot_path
            page.screenshot(path=str(screenshot_path))
            return "submitted", None, screenshot_path

        # 4. Check for Continue / Review button
        cont_btn = None
        for cont_sel in [
            "button:has-text('Continue')",
            "button:has-text('Review your application')",
            "button:has-text('Review')",
        ]:
            try:
                b = page.query_selector(cont_sel)
                if b and b.is_visible() and b.is_enabled():
                    cont_btn = b
                    break
            except Exception:
                pass

        if cont_btn:
            cont_btn.click()
            continue

        break

    return "failed", "Indeed Apply flow did not complete", None
