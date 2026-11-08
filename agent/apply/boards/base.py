"""Base definitions and state tracking for job board applications (LinkedIn, Naukri, Indeed)."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from agent import db
from agent.config import Settings

log = logging.getLogger(__name__)

BoardSite = Literal["linkedin", "naukri", "indeed"]
BoardState = Literal["apply", "needs_human", "blocked", "external", "closed"]


@dataclass
class PageState:
    state: BoardState
    detail: str | None = None
    external_url: str | None = None


def is_site_live(conn: sqlite3.Connection, settings: Settings, site: str) -> bool:
    """True if settings.apply.mode is live, live_enabled:<site> is 1, and no board_blocked flag."""
    if settings.apply.mode != "live":
        return False
    if db.get_flag(conn, f"board_blocked:{site}"):
        return False
    return db.get_flag(conn, f"live_enabled:{site}") == "1"


def prompts_today(conn: sqlite3.Connection, site: str, now: datetime) -> int:
    today_str = now.astimezone(UTC).strftime("%Y-%m-%d")
    val = db.get_flag(conn, f"live_view_prompts:{site}:{today_str}")
    return int(val) if val and val.isdigit() else 0


def record_prompt(conn: sqlite3.Connection, site: str, now: datetime) -> int:
    today_str = now.astimezone(UTC).strftime("%Y-%m-%d")
    current = prompts_today(conn, site, now)
    new_count = current + 1
    db.set_flag(conn, f"live_view_prompts:{site}:{today_str}", str(new_count))
    return new_count


def applications_today(conn: sqlite3.Connection, site: str, now: datetime) -> int:
    start_of_day = now.astimezone(UTC).strftime("%Y-%m-%dT00:00:00Z")
    row = conn.execute(
        "SELECT count(*) FROM apply_attempts WHERE adapter = ? AND mode = 'live'"
        " AND outcome IN ('submitted', 'confirmed') AND started_at >= ?",
        (site, start_of_day),
    ).fetchone()
    return row[0] if row else 0


def can_prompt_today(conn: sqlite3.Connection, site: str, settings: Settings, now: datetime) -> bool:
    return prompts_today(conn, site, now) < settings.apply.human_prompts_per_site_per_day


def can_apply_today(conn: sqlite3.Connection, site: str, settings: Settings, now: datetime) -> bool:
    site_cap = settings.apply.boards.get(site).daily_cap if site in settings.apply.boards else 10
    return applications_today(conn, site, now) < site_cap


def block_site(conn: sqlite3.Connection, site: str, reason: str, wa=None) -> None:
    """Permanently disable a board when account restriction, ID request, or ban text is detected."""
    log.error("Blocking board %s: %s", site, reason)
    db.set_flag(conn, f"live_enabled:{site}", "0")
    db.set_flag(conn, f"board_blocked:{site}", reason)
    if wa is not None:
        try:
            msg = f"⛔ *{site.capitalize()} disabled*: {reason}. Turned off for good; sort out with {site.capitalize()} before turning back on."
            wa.send_text(msg, kind="alert", priority=9)
        except Exception as exc:
            log.warning("failed to send block alert on WhatsApp: %s", exc)
