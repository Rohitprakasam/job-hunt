"""Interview prep pack (written when an application reaches 'interview') and referral drafts.

Both are local files for you to read and send yourself; the agent never contacts anyone."""

from __future__ import annotations

import csv
import logging
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from agent import paths
from agent.config import Profile
from agent.discover.base import norm_company
from agent.match import skills as skill_lib

log = logging.getLogger(__name__)


class PrepPack(BaseModel):
    company_summary: str = Field(max_length=800)
    must_haves: list[str] = Field(max_length=10)
    stories: list[str] = Field(max_length=6)
    likely_questions: list[str] = Field(max_length=12)


SYSTEM = (
    "Prepare an interview pack from the job posting and the candidate's own bullets. company_summary: "
    "what the posting says about the company and team (no outside facts). must_haves: the posting's key "
    "requirements. stories: for each must-have the candidate can show, which bullet to tell as a STAR story "
    "(quote the bullet, add a one-line angle; invent nothing). likely_questions: technical and behavioural "
    "questions this posting suggests. JSON only."
)


def _bullets(profile: Profile) -> list[str]:
    return [b.text for b in profile.all_bullets()]


def fallback(profile: Profile, job) -> PrepPack:
    text = f"{job['title']}\n{job['description'] or ''}"
    must = sorted(skill_lib.find_skills(skill_lib.must_have_section(text), skill_lib.vocabulary(profile)))[
        :10
    ]
    stories = [b for b in _bullets(profile) if any(m in b.lower() for m in must)][:6] or _bullets(profile)[:3]
    questions = [f"Walk me through your experience with {m}." for m in must[:6]] + [
        "Tell me about yourself.",
        f"Why do you want to work at {job['company']}?",
        "Describe a problem you troubleshot end to end.",
        "Tell me about a time you automated a manual task.",
    ]
    return PrepPack(
        company_summary=f"{job['company']} — see the posting.",
        must_haves=must,
        stories=stories,
        likely_questions=questions[:12],
    )


def build_pack(client, profile: Profile, job) -> PrepPack:
    if client is not None:
        content = "BULLETS\n" + "\n".join(f"- {b}" for b in _bullets(profile))
        content += f"\n\nJOB: {job['title']} at {job['company']}\n{(job['description'] or '')[:7000]}"
        try:
            result = client.complete(
                "prep", "strong", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
                max_tokens=6000, json_schema=PrepPack,
            )  # fmt: skip
            if result.data is not None:
                return result.data  # type: ignore[return-value]
        except Exception as exc:
            log.warning("prep pack via LLM failed: %s", type(exc).__name__)
    return fallback(profile, job)


def write_pack(conn: sqlite3.Connection, client, profile: Profile, app_id: int) -> Path:
    job = conn.execute(
        "SELECT j.* FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?", (app_id,)
    ).fetchone()
    pack = build_pack(client, profile, job)
    lines = [f"# Interview prep — {job['title']} at {job['company']}", "", "## Company", "", pack.company_summary,
             "", "## Must-haves", ""]  # fmt: skip
    lines += [f"- {m}" for m in pack.must_haves]
    lines += ["", "## Your stories", ""] + [f"- {s}" for s in pack.stories]
    lines += ["", "## Likely questions", ""] + [f"{i}. {q}" for i, q in enumerate(pack.likely_questions, 1)]
    lines += ["", f"Posting: {job['url'] or job['apply_url'] or '-'}", ""]
    path = paths.output_dir() / "prep" / f"{app_id:05d}-{norm_company(job['company']).replace(' ', '-')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def pending_packs(conn: sqlite3.Connection) -> list[int]:
    """Applications in 'interview' without a pack yet."""
    done = (
        {p.name[:5] for p in (paths.output_dir() / "prep").glob("*.md")}
        if (paths.output_dir() / "prep").exists()
        else set()
    )
    rows = conn.execute("SELECT id FROM applications WHERE status = 'interview'").fetchall()
    return [r["id"] for r in rows if f"{r['id']:05d}" not in done]


# ---------------------------------------------------------------- referrals


def load_contacts(path: Path) -> list[dict[str, str]]:
    """config/contacts.csv with columns name, company, relation, link (gitignored: other people's data)."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(handle)
        ]


def referral_drafts(
    conn: sqlite3.Connection, contacts_path: Path, min_fit: float = 70, limit: int = 15
) -> Path | None:
    contacts = load_contacts(contacts_path)
    if not contacts:
        return None
    by_company: dict[str, list[dict[str, str]]] = {}
    for c in contacts:
        by_company.setdefault(norm_company(c.get("company")), []).append(c)
    jobs = conn.execute(
        "SELECT j.id, j.title, j.company, j.url, j.apply_url, s.fit FROM jobs j JOIN scores s ON s.job_id = j.id"
        " WHERE j.filter_reason IS NULL AND s.fit >= ? ORDER BY s.priority DESC LIMIT 200",
        (min_fit,),
    ).fetchall()
    lines = ["# Referral ideas", "", "Drafts only. Edit and send them yourself.", ""]
    count = 0
    for job in jobs:
        people = by_company.get(norm_company(job["company"]))
        if not people:
            continue
        count += 1
        lines += [
            f"## {job['title']} — {job['company']} (fit {round(job['fit'])})",
            job["url"] or job["apply_url"] or "",
            "",
        ]
        for person in people:
            first = (person.get("name") or "there").split()[0]
            lines += [
                f"**{person.get('name')}** ({person.get('relation') or 'contact'}) {person.get('link') or ''}",
                "",
                f"> Hi {first}, I hope you're well. I saw {job['company']} is hiring a {job['title']} and I'd "
                f"love to apply. Would you be open to referring me, or telling me who the hiring manager is? "
                f"Happy to send my resume. Thank you!",
                "",
            ]
        if count >= limit:
            break
    if not count:
        return None
    path = paths.output_dir() / "referrals.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
