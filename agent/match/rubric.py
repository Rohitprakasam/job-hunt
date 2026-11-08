"""LLM rubric (fast tier): 1-10 fit with three reasons and the gaps. Strict JSON."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent.config import Profile
from agent.match.similarity import profile_text


class Rubric(BaseModel):
    score: int = Field(ge=1, le=10)
    reasons: list[str] = Field(max_length=3)
    gaps: list[str] = Field(max_length=5)
    seniority_fit: Literal["below", "match", "above"]


SYSTEM = (
    "You rate how well a candidate fits a job for an early-career job search. Be strict and honest. "
    "score 1-10: 9-10 strong match on must-haves, 7-8 good, 5-6 partial, 1-4 poor. "
    "reasons: up to 3 short facts from the candidate profile that match the job. "
    "gaps: up to 5 must-have requirements the profile does not show. "
    "seniority_fit: 'above' if the job wants clearly more experience than the candidate has, "
    "'below' if clearly junior to the candidate, else 'match'. Reply with JSON only."
)


def messages(profile: Profile, job) -> list[dict[str, str]]:
    grad = profile.graduation
    years = sum(1 for _ in profile.experience)
    context = (
        f"CANDIDATE PROFILE\nGraduated: {grad.date if grad else 'n/a'} ({grad.degree if grad else ''}). "
        f"Roles held: {years}.\n{profile_text(profile)[:4000]}"
    )
    posting = f"JOB\nTitle: {job['title']}\nCompany: {job['company']}\nLocation: {job['location'] or 'n/a'}\n"
    posting += (job["description"] or "")[:6000]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"{context}\n\n{posting}"}]


def rate(client, profile: Profile, job, run_id: int | None = None) -> tuple[Rubric, str]:
    result = client.complete(
        "rubric", "fast", messages(profile, job), max_tokens=600, json_schema=Rubric, run_id=run_id
    )
    if result.data is None:
        raise ValueError("rubric returned no data")
    return result.data, result.model  # type: ignore[return-value]
