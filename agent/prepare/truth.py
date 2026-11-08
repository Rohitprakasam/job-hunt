"""Truth gate: tailored text may contain no skill, employer, date or number missing from the profile."""

from __future__ import annotations

import re

from agent.config import Profile
from agent.match import skills as skill_lib
from agent.prepare.tailor import Tailored

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
# Tech-looking tokens in a job post (acronyms, CamelCase, names with digits or + # .) that a rewrite
# might copy in as if the candidate had them.
_TERM_RE = re.compile(
    r"\b(?:[A-Z]{2,}[A-Za-z0-9+#]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*|[A-Za-z]+[0-9+#]+[A-Za-z0-9+#]*)\b"
)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER_RE.findall(text or "")}


def profile_corpus(profile: Profile) -> str:
    parts = [profile.summary, *(b.text for b in profile.all_bullets())]
    parts += [s for items in profile.skills.values() for s in items]
    parts += [s for b in profile.all_bullets() for s in b.skills + b.metrics]
    parts += [e.company + " " + e.title for e in profile.experience]
    parts += [p.name + " " + " ".join(p.tags) for p in profile.projects]
    parts += [c.name for c in profile.certifications]
    parts += [ed.institution + " " + ed.degree for ed in profile.education]
    return "\n".join(parts)


def check(profile: Profile, tailored: Tailored, job_text: str = "") -> list[str]:
    """Violations as readable strings; empty list = pass."""
    problems: list[str] = []
    bullets = {b.id: b for b in profile.all_bullets()}
    sections = {e.id for e in profile.experience} | {p.id for p in profile.projects}
    owner = {b.id: item.id for item in [*profile.experience, *profile.projects] for b in item.bullets}
    corpus = profile_corpus(profile)
    corpus_low = corpus.lower()
    have_skills = skill_lib.profile_skills(profile) | skill_lib.find_skills(
        corpus, skill_lib.vocabulary(profile)
    )
    vocab = skill_lib.vocabulary(profile)
    job_terms = {t for t in _TERM_RE.findall(job_text or "") if t.lower() not in corpus_low}

    def scan(where: str, text: str, allowed_numbers: set[str]) -> None:
        extra_numbers = _numbers(text) - allowed_numbers
        if extra_numbers:
            problems.append(f"{where}: numbers not in profile: {', '.join(sorted(extra_numbers))}")
        extra_skills = skill_lib.find_skills(text, vocab) - have_skills
        if extra_skills:
            problems.append(f"{where}: skills not in profile: {', '.join(sorted(extra_skills))}")
        copied = {t for t in job_terms if re.search(rf"(?<![\w+#]){re.escape(t)}(?![\w+#])", text)}
        if copied:
            problems.append(f"{where}: terms from the job not in profile: {', '.join(sorted(copied))}")

    for section in [*tailored.experience, *tailored.projects]:
        if section.id not in sections:
            problems.append(f"unknown section id {section.id}")
            continue
        for bullet in section.bullets:
            original = bullets.get(bullet.id)
            if original is None:
                problems.append(f"unknown bullet id {bullet.id}")
                continue
            if owner[bullet.id] != section.id:
                problems.append(f"bullet {bullet.id} moved to section {section.id}")
            scan(
                f"bullet {bullet.id}",
                bullet.text,
                _numbers(original.text) | _numbers(" ".join(original.metrics)),
            )
    scan("summary", tailored.summary, _numbers(corpus))
    for skill in tailored.skills:
        if skill_lib.canon(skill) not in have_skills and skill.lower() not in corpus_low:
            problems.append(f"skill not in profile: {skill}")
    missing = {e.id for e in profile.experience} - {s.id for s in tailored.experience}
    if missing:
        problems.append(f"experience dropped: {', '.join(sorted(missing))}")
    return problems
