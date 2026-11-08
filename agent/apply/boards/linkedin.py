"""LinkedIn Easy Apply adapter.

Handles:
- Logged-in / checkpoint / captcha detection -> needs_human
- Account restriction / verification -> blocked
- Job closed / plain Apply (external link) -> external
- Multi-step modal: contact, resume upload, questions from answer bank/profile, untick follow, submit
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

EASY_APPLY_SELECTORS = [
    "button.jobs-apply-button:has-text('Easy Apply')",
    "button[aria-label*='Easy Apply']",
    "button:has-text('Easy Apply')",
]

EXTERNAL_APPLY_SELECTORS = [
    "button.jobs-apply-button:has-text('Apply')",
    "a[aria-label*='Apply on company website']",
    "a:has-text('Apply on company website')",
    "a.jobs-apply-button",
]


def detect_state(page) -> PageState:
    url = page.url.lower()
    page_text = page.inner_text("body").lower()

    # 1. Blocked / restricted
    blocked_keywords = [
        "account has been restricted",
        "identity verification",
        "verify your identity",
        "unusual activity detected",
        "your account is temporarily locked",
        "we noticed unusual activity",
    ]
    if any(k in page_text for k in blocked_keywords):
        return PageState("blocked", detail="LinkedIn account restriction or identity verification detected")

    # 2. Needs human (checkpoint, captcha, login, authwall)
    checkpoint_indicators = [
        "checkpoint/challenge",
        "/login",
        "/authwall",
        "uas/login",
        "security verification",
        "solve this puzzle",
        "enter the code we sent",
    ]
    if any(k in url or k in page_text for k in checkpoint_indicators):
        return PageState("needs_human", detail="LinkedIn login or security checkpoint")

    # 3. Job closed / already applied
    if "no longer accepting applications" in page_text or "this job is closed" in page_text:
        return PageState("closed", detail="Job is closed or no longer accepting applications")
    if "applied " in page_text or "application submitted" in page_text:
        return PageState("closed", detail="Already applied")

    # 4. Check for Easy Apply button
    for sel in EASY_APPLY_SELECTORS:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                return PageState("apply")
        except Exception:
            pass

    # 5. Check for plain external Apply button
    for sel in EXTERNAL_APPLY_SELECTORS:
        try:
            elem = page.query_selector(sel)
            if elem and elem.is_visible():
                href = elem.get_attribute("href")
                return PageState(
                    "external", detail="Plain apply redirects to company site", external_url=href
                )
        except Exception:
            pass

    return PageState("closed", detail="No active Easy Apply or external apply button found")


def apply(
    page,
    conn: sqlite3.Connection,
    profile: Profile,
    client,
    app,
    application_dir: Path,
    confirmation_timeout_s: int = 30,
) -> tuple[str, str | None, Path | None]:
    """Execute LinkedIn Easy Apply flow. Returns (outcome, error, screenshot_path)."""
    # Click Easy Apply
    clicked = False
    for sel in EASY_APPLY_SELECTORS:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        return "failed", "could not click Easy Apply button", None

    modal_selector = (
        "dialog, div[role='dialog'], [role='dialog'], div.jobs-easy-apply-modal, .jobs-easy-apply-modal"
    )
    try:
        page.wait_for_selector(modal_selector, timeout=8000)
    except Exception:
        return "failed", "Easy Apply modal did not open", None

    max_steps = 12
    step = 0
    screenshot_path = application_dir / "confirmation-linkedin.png"

    while step < max_steps:
        step += 1
        page.wait_for_timeout(1000)
        modal_text = ""
        try:
            modal_text = page.inner_text(modal_selector).lower()
        except Exception:
            pass

        # Check confirmation
        if (
            "application sent" in modal_text
            or "your application was sent" in modal_text
            or "application was sent" in modal_text
        ):
            page.screenshot(path=str(screenshot_path))
            return "confirmed", None, screenshot_path

        # 1. Resume upload if present
        try:
            resume_path = app["resume_path"] if "resume_path" in app.keys() and app["resume_path"] else None
            if resume_path and os.path.exists(resume_path):
                file_inputs = page.query_selector_all(f"{modal_selector} input[type='file']")
                for fi in file_inputs:
                    fi.set_input_files(str(resume_path))
                    page.wait_for_timeout(1000)
                if not file_inputs:
                    upload_btn = page.query_selector(
                        f"{modal_selector} button:has-text('Upload resume'), {modal_selector} [aria-label*='Upload resume']"
                    )
                    if upload_btn and upload_btn.is_visible():
                        with page.expect_file_chooser(timeout=5000) as fc_info:
                            upload_btn.click()
                        fc_info.value.set_files(str(resume_path))
                        page.wait_for_timeout(2000)
        except Exception as file_exc:
            log.warning("error attaching resume on LinkedIn: %s", file_exc)

        # 2. Form inputs in current modal step
        try:
            raw_fields = page.evaluate(form.EXTRACT_JS)
            fields = [form.Field.from_js(rf) for rf in raw_fields if rf.get("label") != "Select language"]
            if fields:
                resume_p = (
                    Path(app["resume_path"]) if "resume_path" in app.keys() and app["resume_path"] else None
                )
                cover_p = (
                    Path(app["cover_path"]) if "cover_path" in app.keys() and app["cover_path"] else None
                )
                planned = form.plan_fields(
                    fields, conn, profile, client, resume_p, cover_p, app["job_id"], app.get("company", "")
                )
                # Answer gate check
                for f in planned:
                    if f.field.required and (f.plan.value is None or str(f.plan.value).strip() == ""):
                        answers.record_pending(conn, f.field.label, None)
                        return "skipped", f"unanswerable required question: {f.field.label}", None
                form.fill(page, planned)
        except Exception as form_exc:
            log.warning("error planning or filling modal step on LinkedIn: %s", form_exc)

        # 3. Untick 'Follow <company>' if present
        try:
            checkboxes = page.query_selector_all(f"{modal_selector} input[type='checkbox']")
            for cb in checkboxes:
                parent_text = cb.evaluate("el => el.closest('label, div')?.innerText || ''").lower()
                if "follow" in parent_text and cb.is_checked():
                    cb.uncheck()
        except Exception:
            pass

        # 4. Check for Submit button
        submit_btn = None
        for submit_sel in [
            "button:has-text('Submit application')",
            "button[aria-label='Submit application']",
            "button:has-text('Submit')",
        ]:
            try:
                b = page.query_selector(f"{modal_selector} {submit_sel}")
                if b and b.is_visible() and b.is_enabled():
                    submit_btn = b
                    break
            except Exception:
                pass

        if submit_btn:
            submit_btn.click()
            # Wait for confirmation text
            for _ in range(int(confirmation_timeout_s * 2)):
                page.wait_for_timeout(500)
                body_text = page.inner_text("body").lower()
                if (
                    "application sent" in body_text
                    or "your application was sent" in body_text
                    or "application was sent" in body_text
                ):
                    page.screenshot(path=str(screenshot_path))
                    return "confirmed", None, screenshot_path
            # Take screenshot anyway
            page.screenshot(path=str(screenshot_path))
            return "submitted", None, screenshot_path

        # 5. Check for Next / Review button
        next_btn = None
        for next_sel in [
            "button:has-text('Next')",
            "button:has-text('Review')",
            "button[aria-label='Continue to next step']",
            "button[aria-label='Review your application']",
        ]:
            try:
                b = page.query_selector(f"{modal_selector} {next_sel}")
                if b and b.is_visible() and b.is_enabled():
                    next_btn = b
                    break
            except Exception:
                pass

        if next_btn:
            next_btn.click()
            continue

        # If modal is stuck
        break

    return "failed", "modal flow did not complete (stuck or unhandled step)", None
