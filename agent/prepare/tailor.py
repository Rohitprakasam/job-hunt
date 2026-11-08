"""Tailoring: choose, order and reword profile bullets toward one job. The LLM only ever sees and
returns bullet ids and text; employers, titles, dates and contact details come from profile.yaml."""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from agent.config import Profile
from agent.match import skills as skill_lib

log = logging.getLogger(__name__)


class TBullet(BaseModel):
    id: str
    text: str = Field(max_length=400)


class TSection(BaseModel):
    id: str
    bullets: list[TBullet]


class Tailored(BaseModel):
    summary: str = Field(max_length=700)
    experience: list[TSection]
    projects: list[TSection] = []
    skills: list[str]
    method: str = "llm"


SYSTEM = (
    "You tailor a resume to one job. Rules, all strict:\n"
    "1. Use only the candidate's bullets, referenced by id. You may drop, reorder and reword them toward "
    "the job's own terms.\n"
    "2. Never add a skill, tool, employer, title, date, number or claim that is not in that bullet. Keep "
    "every number exactly as written.\n"
    "3. skills: choose and order only from AVAILABLE SKILLS, most relevant first.\n"
    "4. summary: 2-3 sentences built only from the given summary and bullets.\n"
    "5. Keep at least one bullet per experience. Reply with JSON only."
)


def _profile_payload(profile: Profile) -> str:
    lines = [f"SUMMARY: {profile.summary}", "EXPERIENCE:"]
    for e in profile.experience:
        lines.append(f"- section {e.id} ({e.title}):")
        lines += [f"    {b.id}: {b.text}" for b in e.bullets]
    if profile.projects:
        lines.append("PROJECTS:")
        for p in profile.projects:
            lines.append(f"- section {p.id} ({p.name}):")
            lines += [f"    {b.id}: {b.text}" for b in p.bullets]
    lines.append("AVAILABLE SKILLS: " + ", ".join(all_skills(profile)))
    return "\n".join(lines)


def all_skills(profile: Profile) -> list[str]:
    seen, out = set(), []
    for items in profile.skills.values():
        for s in items:
            if s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return out


def with_llm(client, profile: Profile, job, run_id: int | None = None) -> Tailored:
    content = f"{_profile_payload(profile)}\n\nJOB: {job['title']} at {job['company']}\n{(job['description'] or '')[:6000]}"
    result = client.complete(
        "tailor", "strong", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
        max_tokens=8000, json_schema=Tailored, run_id=run_id,
    )  # fmt: skip
    if result.data is None:
        raise ValueError("tailor returned no data")
    return result.data  # type: ignore[return-value]


def _overlap(text: str, terms: set[str]) -> int:
    low = text.lower()
    return sum(1 for t in terms if t in low)


def deterministic(profile: Profile, job) -> Tailored:
    """No LLM: reorder bullets and skills by overlap with the job. True by construction."""
    job_text = f"{job['title']}\n{job['description'] or ''}"
    terms = skill_lib.find_skills(job_text, skill_lib.vocabulary(profile))
    terms |= {w for w in re.findall(r"[a-z][a-z0-9+#.]{3,}", job_text.lower())}

    def section(item) -> TSection:
        ranked = sorted(item.bullets, key=lambda b: -_overlap(b.text, terms))
        return TSection(id=item.id, bullets=[TBullet(id=b.id, text=b.text) for b in ranked])

    ranked_skills = sorted(all_skills(profile), key=lambda s: (skill_lib.canon(s) not in terms, s.lower()))
    return Tailored(
        summary=profile.summary,
        experience=[section(e) for e in profile.experience],
        projects=[section(p) for p in profile.projects],
        skills=ranked_skills,
        method="deterministic",
    )
