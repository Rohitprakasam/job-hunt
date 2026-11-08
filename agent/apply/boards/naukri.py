"""Naukri application adapter.

Handles:
- Logged-in / OTP / Captcha detection -> needs_human
- Account restriction -> blocked
- Apply on company site -> external
- One-click apply or questions drawer
- Confirmation detection and proof screenshot
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from agent.apply import form
from agent.apply.boards.base import PageState
from agent.config import Profile
from agent.prepare import answers

log = logging.getLogger(__name__)

APPLY_BUTTON_SELECTORS = [
    "button.apply-button:has-text('Apply')",
    "#apply-button",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
]

EXTERNAL_APPLY_SELECTORS = [
    "button:has-text('Apply on company site')",
    "a:has-text('Apply on company site')",
    "button:has-text('Apply on website')",
    "a:has-text('Apply on website')",
]


def detect_state(page) -> PageState:
    url = page.url.lower()
    page_text = page.inner_text("body").lower()

    # 1. Blocked / restricted
    if (
        "account has been blocked" in page_text
        or "account is suspended" in page_text
        or "access denied" in page_text
        or "temporarily restricted" in page_text
    ):
        return PageState("blocked", detail="Naukri account blocked or restricted")

    # 2. Needs human (login, OTP, captcha)
    if (
        "naukri.com/nlogin" in url
        or "/login" in url
        or "enter otp" in page_text
        or "otp sent" in page_text
        or "verify your mobile" in page_text
        or "solve the puzzle" in page_text
    ):
        return PageState("needs_human", detail="Naukri login or OTP verification")

    # 3. Closed / already applied
    if "already applied" in page_text or "you have applied" in page_text:
        return PageState("closed", detail="Already applied")
    if "this job has expired" in page_text or "no longer active" in page_text:
        return PageState("closed", detail="Job has expired")

    # 4. External apply
    for sel in EXTERNAL_APPLY_SELECTORS:
        try:
            elem = page.query_selector(sel)
            if elem and elem.is_visible():
                href = elem.get_attribute("href")
                return PageState("external", detail="Apply on company site", external_url=href)
        except Exception:
            pass

    # 5. Native apply
    for sel in APPLY_BUTTON_SELECTORS:
        try:
            elem = page.query_selector(sel)
            if elem and elem.is_visible():
                return PageState("apply")
        except Exception:
            pass

    return PageState("closed", detail="No active apply button found on Naukri")


def apply(
    page,
    conn: sqlite3.Connection,
    profile: Profile,
    client,
    app,
    application_dir: Path,
    confirmation_timeout_s: int = 30,
) -> tuple[str, str | None, Path | None]:
    """Execute Naukri apply flow. Returns (outcome, error, screenshot_path)."""
    clicked = False
    for sel in APPLY_BUTTON_SELECTORS:
        try:
            elem = page.query_selector(sel)
            if elem and elem.is_visible():
                elem.click()
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        return "failed", "could not click Naukri Apply button", None

    screenshot_path = application_dir / "confirmation-naukri.png"
    page.wait_for_timeout(2000)

    # Check if a chatbot/drawer with questions opens
    drawer_sel = "div.apply-message-container, div.chatbot-container, div[role='dialog']"
    drawer = None
    try:
        drawer = page.query_selector(drawer_sel)
    except Exception:
        pass

    if drawer and drawer.is_visible():
        try:
            fields = form.find_fields(page, drawer_sel)
            if fields:
                planned = form.plan_fields(
                    fields, conn, profile, client, None, None, app["job_id"], app.get("company", "")
                )
                for f in planned:
                    if f.field.required and (f.value is None or str(f.value).strip() == ""):
                        answers.record_pending(conn, f.field.label, None)
                        return "skipped", f"unanswerable required question: {f.field.label}", None
                form.fill_form(page, planned)

                # Submit drawer
                sub_btn = page.query_selector(
                    f"{drawer_sel} button[type='submit'], {drawer_sel} button:has-text('Save'), {drawer_sel} button:has-text('Submit')"
                )
                if sub_btn:
                    sub_btn.click()
        except Exception as exc:
            log.warning("error in Naukri drawer filling: %s", exc)

    # Wait for confirmation text
    for _ in range(int(confirmation_timeout_s * 2)):
        page.wait_for_timeout(500)
        body = page.inner_text("body").lower()
        if (
            "you have successfully applied" in body
            or "application sent" in body
            or "applied successfully" in body
            or "applied on" in body
        ):
            page.screenshot(path=str(screenshot_path))
            return "confirmed", None, screenshot_path

    page.screenshot(path=str(screenshot_path))
    return "submitted", None, screenshot_path
