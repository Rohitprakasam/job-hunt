"""The seven automatic gates that replace a human review. A job that fails any gate is skipped and
logged, never sent half-right. Each gate is a pure function returning a GateResult."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agent import db
from agent.config import ApplySettings, Profile
from agent.discover.base import norm_company
from agent.prepare import truth
from agent.prepare.tailor import Tailored


@dataclass
class GateResult:
    gate: str
    ok: bool
    detail: str = ""


def as_json(results: list[GateResult]) -> list[dict]:
    return [asdict(r) for r in results]


# 1 ---------------------------------------------------------------- fit


def fit_gate(fit: float | None, filter_reason: str | None, settings: ApplySettings) -> GateResult:
    if filter_reason:
        return GateResult("fit", False, f"hard filter: {filter_reason}")
    if fit is None or fit < settings.min_fit:
        return GateResult("fit", False, f"fit {fit} below {settings.min_fit}")
    return GateResult("fit", True, f"fit {fit}")


# 2 ---------------------------------------------------------------- truth


def truth_gate(
    profile: Profile, tailored: Tailored, job_text: str, cover: str = "", names: tuple[str, ...] = ()
) -> GateResult:
    """`names` (the job title and company) may appear in the cover note: naming the role is not a claim."""
    problems = truth.check(profile, tailored, job_text)
    for name in sorted(filter(None, names), key=len, reverse=True):
        cover = cover.replace(name, " ")
    if cover.strip():
        problems += [
            f"cover: {p}"
            for p in truth.check(profile, _as_summary(tailored, cover), job_text)
            if p.startswith("summary")
        ]
    return GateResult("truth", not problems, "; ".join(problems[:5]))


def _as_summary(tailored: Tailored, text: str) -> Tailored:
    return tailored.model_copy(update={"summary": text})


# 3 ---------------------------------------------------------------- answers


@dataclass
class FieldPlan:
    """One form field and what the agent will put in it."""

    label: str
    required: bool
    kind: str  # text | textarea | select | radio | checkbox | file | email | tel | url | number
    value: str | None
    source: str | None  # profile | bank | llm | file | None
    category: str = "factual"
    options: list[str] | None = None


def answer_gate(fields: list[FieldPlan]) -> GateResult:
    missing = [f.label for f in fields if f.required and not f.value]
    if missing:
        return GateResult("answers", False, "no answer for: " + "; ".join(missing[:5]))
    bad_legal = [
        f.label for f in fields if f.category == "legal" and f.value and f.source not in ("bank", "profile")
    ]
    if bad_legal:
        return GateResult("answers", False, "legal question not from saved answers: " + "; ".join(bad_legal))
    return GateResult("answers", True, f"{sum(1 for f in fields if f.value)} fields")


# 4 ---------------------------------------------------------------- form read-back


def form_gate(intended: dict[str, str], actual: dict[str, str], validation_errors: list[str]) -> GateResult:
    if validation_errors:
        return GateResult("form", False, "validation: " + "; ".join(validation_errors[:3]))
    wrong = [label for label, value in intended.items() if _clean(actual.get(label)) != _clean(value)]
    if wrong:
        return GateResult("form", False, "fields not holding intended value: " + "; ".join(wrong[:5]))
    return GateResult("form", True, f"{len(intended)} fields verified")


def _clean(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


# 5 ---------------------------------------------------------------- proof


def proof_gate(screenshot: Path | None, values_saved: bool) -> GateResult:
    if not screenshot or not screenshot.exists() or screenshot.stat().st_size == 0:
        return GateResult("proof", False, "no screenshot before submit")
    if not values_saved:
        return GateResult("proof", False, "submitted values not saved")
    return GateResult("proof", True, screenshot.name)


# 6 ---------------------------------------------------------------- volume


def volume_gate(
    conn: sqlite3.Connection,
    company: str,
    settings: ApplySettings,
    now: datetime,
    adapter: str | None = None,
) -> GateResult:
    day = db.to_iso(now - timedelta(hours=24))
    today = conn.execute(
        "SELECT count(*) FROM applications WHERE origin = 'agent' AND submitted_at >= ?", (day,)
    ).fetchone()[0]
    if today >= settings.daily_cap:
        return GateResult("volume", False, f"daily cap {settings.daily_cap} reached")
    if settings.per_company_days:
        since = db.to_iso(now - timedelta(days=settings.per_company_days))
        rows = conn.execute(
            "SELECT j.company FROM applications a JOIN jobs j ON j.id = a.job_id"
            " WHERE a.submitted_at >= ? AND a.status NOT IN ('prepared', 'skipped')",
            (since,),
        ).fetchall()
        if norm_company(company) in {norm_company(r["company"]) for r in rows}:
            return GateResult(
                "volume", False, f"applied to {company} in the last {settings.per_company_days} days"
            )
    if consecutive_failures(conn, adapter=adapter) >= settings.max_consecutive_failures:
        msg = (
            f"{settings.max_consecutive_failures} submissions failed in a row for {adapter}"
            if adapter
            else f"{settings.max_consecutive_failures} submissions failed in a row"
        )
        return GateResult("volume", False, msg)
    return GateResult("volume", True, f"{today}/{settings.daily_cap} today")


def consecutive_failures(conn: sqlite3.Connection, adapter: str | None = None) -> int:
    enabled_at = db.get_flag(conn, f"live_enabled_at:{adapter}") if adapter else None
    if adapter and enabled_at:
        rows = conn.execute(
            "SELECT outcome FROM apply_attempts WHERE mode = 'live' AND adapter = ? AND started_at >= ?"
            " AND outcome IN ('submitted', 'confirmed', 'failed') ORDER BY id DESC LIMIT 10",
            (adapter, enabled_at),
        ).fetchall()
    elif adapter:
        rows = conn.execute(
            "SELECT outcome FROM apply_attempts WHERE mode = 'live' AND adapter = ? AND outcome IN ('submitted', 'confirmed', 'failed')"
            " ORDER BY id DESC LIMIT 10",
            (adapter,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT outcome FROM apply_attempts WHERE mode = 'live' AND outcome IN ('submitted', 'confirmed', 'failed')"
            " ORDER BY id DESC LIMIT 10"
        ).fetchall()
    count = 0
    for row in rows:
        if row["outcome"] != "failed":
            break
        count += 1
    return count


# 7 ---------------------------------------------------------------- block


BLOCK_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "cf-challenge", "verify you are human", "are you a robot",
    "sign in to apply", "log in to apply", "create an account", "please log in",
)  # fmt: skip


def block_gate(page_text: str, frame_urls: list[str], unknown_steps: bool) -> GateResult:
    low = page_text.lower()
    hits = [m for m in BLOCK_MARKERS if m in low]
    hits += [u for u in frame_urls if "captcha" in u or "challenges.cloudflare" in u]
    if hits:
        return GateResult("block", False, "blocked: " + ", ".join(sorted(set(hits))[:3]))
    if unknown_steps:
        return GateResult("block", False, "unknown multi-page flow")
    return GateResult("block", True)
