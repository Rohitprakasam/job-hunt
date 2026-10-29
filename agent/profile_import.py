"""One-time import of the portfolio data into config/profile.draft.yaml for the user to review.

The portfolio's Mongo collections can be written without auth, so the import is never synced at
runtime: profile.yaml (reviewed by the user) is the only truth source. A hash of the imported
source is kept so a later re-import that differs is shown and needs confirmation.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dateutil import parser as dateparser

from agent import db

HASH_FLAG = "profile_import_hash"
TODO = "TODO"

_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff\U0000fe0f\U0000200d]+", re.UNICODE
)
_RANGE_SPLIT_RE = re.compile(r"\s+[-–—]\s+|\s*[–—]\s*")


def clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", text)).strip()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"


def parse_month(text: str) -> str | None:
    text = text.strip()
    if not text or text.lower() in {"present", "current", "now", "ongoing"}:
        return None
    parsed = dateparser.parse(text, default=dateparser.parse("2000-01-01"))
    return f"{parsed:%Y-%m}"


def parse_duration(duration: str | None) -> tuple[str | None, str | None]:
    """'Mar 2024 – Apr 2024' -> ('2024-03', '2024-04'); 'Jun 2024 - Present' -> ('2024-06', None)."""
    if not duration:
        return None, None
    parts = _RANGE_SPLIT_RE.split(duration.strip(), maxsplit=1)
    start = parse_month(parts[0])
    end = parse_month(parts[1]) if len(parts) > 1 else None
    return start, end


def _handle(url: str) -> str | None:
    path = re.sub(r"^https?://[^/]+/?", "", url).split("?")[0].strip("/")
    return path.split("/")[-1] or None


def load_portfolio_ts(path: Path) -> dict[str, Any]:
    """Extract the `portfolioData = {...}` object literal (the file is JSON-shaped) from portfolio.ts."""
    source = path.read_text(encoding="utf-8")
    marker = source.find("portfolioData")
    start = source.find("{", marker)
    if marker < 0 or start < 0:
        raise ValueError("portfolioData object not found")
    depth, in_string, escape = 0, False, False
    for index in range(start, len(source)):
        char = source[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(source[start : index + 1])
    raise ValueError("portfolioData object is not closed")


def source_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def to_draft(data: dict[str, Any]) -> dict[str, Any]:
    hero = data.get("heroSection", {}) or {}
    contact = data.get("contact", {}) or {}
    full_name = clean(hero.get("fullName")) or TODO
    names = full_name.split()
    # "Rohit Prakasam G": a trailing initial is not the surname, but the spellings with and
    # without it are both name variants the redactor must catch.
    has_initial = len(names) > 2 and len(names[-1].rstrip(".")) == 1
    last_name = " ".join(names[1:-1] if has_initial else names[1:])
    variants = [" ".join(names[:-1]), " ".join(names[1:])] if has_initial else []

    links: list[dict[str, Any]] = []
    for label, key, on_resume in (
        ("LinkedIn", "linkedin", True),
        ("GitHub", "github", True),
        ("Instagram", "instagram", False),
        ("WhatsApp", "whatsapp", False),
    ):
        url = contact.get(key)
        if url and url.startswith("http"):
            links.append({"label": label, "url": url, "handle": _handle(url), "on_resume": on_resume})

    phone_digits = re.sub(r"\D", "", contact.get("phone", "") or "")
    experience = []
    for item in data.get("experience", []) or []:
        company = clean(item.get("organization"))
        exp_id = slug(company)
        start, end = parse_duration(item.get("duration"))
        experience.append(
            {
                "id": exp_id,
                "company": company,
                "title": clean(item.get("role")),
                "start": start or TODO,
                "end": end,
                "location": clean(item.get("location")) or None,
                "bullets": [
                    {"id": f"{exp_id}-{n:02d}", "text": clean(text), "skills": [], "metrics": []}
                    for n, text in enumerate(item.get("highlights", []) or [], start=1)
                ],
            }
        )

    education = []
    for item in data.get("education", []) or []:
        start, end = parse_duration(item.get("duration"))
        education.append(
            {
                "institution": clean(item.get("institution")),
                "degree": clean(item.get("degree")),
                "start": start,
                "end": end,
                "score": clean(item.get("score")) or None,
            }
        )

    projects = []
    for item in data.get("projects", []) or []:
        project_id = slug(item.get("title", ""))
        link = item.get("link")
        projects.append(
            {
                "id": project_id,
                "name": clean(item.get("title")),
                "url": link if link and link.startswith("http") else None,
                "tags": list(item.get("tags", []) or []),
                "bullets": [{"id": f"{project_id}-01", "text": clean(item.get("description")), "skills": []}],
            }
        )

    first_degree = education[0] if education else None
    return {
        "contact": {
            "full_name": full_name,
            "first_name": names[0] if names else TODO,
            "last_name": last_name or TODO,
            "name_variants": variants,
            "email": contact.get("email") or TODO,
            "phone_e164": f"+{phone_digits}" if len(phone_digits) >= 11 else TODO,
            "city": TODO,
            "country": TODO,
            "links": links,
        },
        "summary": clean((data.get("aboutMe") or {}).get("description")) or TODO,
        "work_authorization": {"countries": [TODO], "needs_sponsorship": False},
        "notice_period_days": TODO,
        "graduation": {
            "degree": first_degree["degree"],
            "institution": first_degree["institution"],
            "date": first_degree["end"] or TODO,
        }
        if first_degree
        else None,
        "salary_expectation": {"currency": "INR", "min": TODO, "target": TODO},
        "experience": experience,
        "education": education,
        "skills": {
            clean(group.get("category")): [clean(s) for s in group.get("items", []) or []]
            for group in data.get("skills", []) or []
        },
        "projects": projects,
        "certifications": [{"name": clean(c.get("title"))} for c in data.get("certifications", []) or []],
    }


@dataclass
class ImportResult:
    path: Path
    written: bool
    changed_since_last: bool
    todos: int


def write_draft(
    conn: sqlite3.Connection, data: dict[str, Any], path: Path, confirm_changed: bool = False
) -> ImportResult:
    digest = source_hash(data)
    previous = db.get_flag(conn, HASH_FLAG)
    changed = previous is not None and previous != digest
    draft = to_draft(data)
    todos = yaml.safe_dump(draft).count(TODO)
    if changed and not confirm_changed:
        return ImportResult(path, written=False, changed_since_last=True, todos=todos)
    header = (
        "# DRAFT imported from the portfolio. Review every line, fill each TODO, fix wording,\n"
        "# add skills/metrics to bullets, then save as profile.yaml. Only true facts.\n"
    )
    path.write_text(header + yaml.safe_dump(draft, sort_keys=False, allow_unicode=True), encoding="utf-8")
    db.set_flag(conn, HASH_FLAG, digest)
    return ImportResult(path, written=True, changed_since_last=changed, todos=todos)
