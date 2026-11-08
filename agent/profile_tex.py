"""Build the profile draft from your LaTeX resume (config/resume.tex), so form answers, scoring and the
truth checks use the same facts as the resume you send.

Reads \\role{Title}{Organisation | details}{Dates} entries with their \\item bullets, the
"Technical Skills" lines, the summary, certifications and the header contact line. Fields the resume
does not have (work authorization, notice period, salary) are kept from the existing draft or left TODO.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from agent.prepare.latex import SKILL_LINE_RE, SKILLS_RE, SUMMARY_RE, first_section, plain, split_top
from agent.profile_import import TODO, clean, parse_duration, slug

_SECTION_RE = re.compile(r"\\sectionhead(?:\[[^\]]*\])?\{([^}]*)\}")
_PHONE_RE = re.compile(r"\+\d[\d ]{8,}\d")


def _groups(text: str, start: int, count: int) -> tuple[list[str], int]:
    """Read `count` balanced {...} arguments starting at `start`. Returns (args, end)."""
    args, i = [], start
    for _ in range(count):
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if i >= len(text) or text[i] != "{":
            raise ValueError("expected {")
        depth, j = 0, i
        while j < len(text):
            if text[j] == "{" and text[j - 1] != "\\":
                depth += 1
            elif text[j] == "}" and text[j - 1] != "\\":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        args.append(text[i + 1 : j])
        i = j + 1
    return args, i


def sections(tex: str) -> dict[str, str]:
    marks = list(_SECTION_RE.finditer(tex))
    end_doc = tex.find(r"\end{document}")
    out = {}
    for n, mark in enumerate(marks):
        stop = marks[n + 1].start() if n + 1 < len(marks) else (end_doc if end_doc > 0 else len(tex))
        out[mark.group(1).strip().lower()] = tex[mark.end() : stop]
    return out


def roles(block: str) -> list[dict[str, Any]]:
    """Each \\role with the \\item bullets that follow it (up to the next \\role)."""
    found = []
    starts = [m.start() for m in re.finditer(r"\\role\b", block)]
    for n, start in enumerate(starts):
        try:
            (title, org, dates), end = _groups(block, start + len(r"\role"), 3)
        except ValueError:
            continue
        stop = starts[n + 1] if n + 1 < len(starts) else len(block)
        body = block[end:stop]
        bullets = [clean(plain(b)) for b in re.split(r"\\item\b", body)[1:]]
        bullets = [re.sub(r"\s*\\end\{itemize\}.*", "", b, flags=re.S).strip() for b in bullets]
        found.append({"title": clean(plain(title)), "org": plain(org), "org_tex": org, "dates": plain(dates),
                      "bullets": [b for b in bullets if b]})  # fmt: skip
    return found


def _org_parts(org: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in org.split("|") if p.strip()]
    company = re.sub(r"\s*\(.*?\)", "", parts[0]).strip() if parts else org
    location = next(
        (
            p
            for p in parts[1:]
            if re.search(r"chennai|bengaluru|bangalore|india|remote|hyderabad|pune|mumbai", p, re.I)
        ),
        None,
    )
    return company, location


def to_draft(tex: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    prev = previous or {}
    secs = sections(tex)
    header = tex[max(0, tex.find(r"\begin{document}")) : first_section(tex)]
    contact = dict(prev.get("contact") or {})
    location_line = re.search(r"([A-Z][a-zA-Z]+),\s*([A-Z][a-zA-Z]+)\s*\$\|\$", header)
    if location_line:
        contact["city"], contact["country"] = location_line.group(1), location_line.group(2)
    phone = _PHONE_RE.search(header)
    if phone and contact.get("phone_e164", TODO) == TODO:
        contact["phone_e164"] = "+" + re.sub(r"\D", "", phone.group(0))
    email = re.search(r"mailto:([^}\s]+)", header)
    if email and contact.get("email", TODO) == TODO:
        contact["email"] = email.group(1)

    experience = []
    for role in roles(next((v for k, v in secs.items() if "experience" in k), "")):
        company, location = _org_parts(role["org"])
        start, end = parse_duration(role["dates"])
        exp_id = slug(company)
        experience.append({
            "id": exp_id, "company": company, "title": role["title"], "start": start or TODO, "end": end,
            "location": location,
            "bullets": [{"id": f"{exp_id}-{i:02d}", "text": b, "skills": [], "metrics": []}
                        for i, b in enumerate(role["bullets"], 1)],
        })  # fmt: skip

    projects = []
    for role in roles(next((v for k, v in secs.items() if "project" in k), "")):
        name = clean(re.sub(r":.*", "", role["title"])) or role["title"]
        pid = slug(name)
        link = re.search(r"https?://[^}\s]+", role["org_tex"])
        projects.append({
            "id": pid, "name": role["title"], "url": link.group(0) if link else None,
            "tags": [t for t in split_top(role["org"].split("|")[0]) if len(t) < 40],
            "bullets": [{"id": f"{pid}-{i:02d}", "text": b, "skills": []} for i, b in enumerate(role["bullets"], 1)],
        })  # fmt: skip

    education = []
    for role in roles(next((v for k, v in secs.items() if "education" in k), "")):
        parts = [p.strip() for p in role["org"].split("|")]
        start, end = parse_duration(role["dates"])
        end = end or start  # a single year is a finished year, not "present"
        education.append({
            "institution": parts[0], "degree": role["title"], "start": start, "end": end,
            "score": " ".join(parts[1:]).strip() or None,
        })  # fmt: skip

    skills: dict[str, list[str]] = {}
    block = SKILLS_RE.search(tex)
    for line in SKILL_LINE_RE.finditer(block.group(2) if block else ""):
        skills[plain(line.group(2)).rstrip(":").strip()] = [plain(s) for s in split_top(line.group(3))]

    certs_block = next((v for k, v in secs.items() if "certification" in k), "")
    certs = [
        {"name": c.strip()}
        for c in plain(
            re.sub(r"\\begin\{adjustwidth\}\{[^}]*\}\{[^}]*\}|\\end\{adjustwidth\}", "", certs_block)
        ).split("|")
        if c.strip()
    ]

    summary_match = SUMMARY_RE.search(tex)
    summary = clean(plain(summary_match.group(2))) if summary_match else prev.get("summary", TODO)

    graduation = prev.get("graduation")
    if education and education[0].get("end") and not graduation:
        graduation = {
            "degree": education[0]["degree"],
            "institution": education[0]["institution"],
            "date": education[0]["end"],
        }
    return {
        "contact": contact or {"full_name": TODO},
        "summary": summary,
        "work_authorization": prev.get("work_authorization")
        or {"countries": [TODO], "needs_sponsorship": False},
        "notice_period_days": prev.get("notice_period_days", TODO),
        "graduation": graduation,
        "salary_expectation": prev.get("salary_expectation")
        or {"currency": "INR", "min": TODO, "target": TODO},
        "experience": experience,
        "education": education,
        "skills": skills,
        "projects": projects,
        "certifications": certs,
    }


def write(tex_path: Path, draft_path: Path) -> tuple[Path, int, Path | None]:
    """Write the draft; the previous draft is kept as .bak. Returns (path, TODO count, backup)."""
    previous = yaml.safe_load(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else None
    backup = None
    if draft_path.exists():
        backup = draft_path.with_suffix(".yaml.bak")
        backup.write_text(draft_path.read_text(encoding="utf-8"), encoding="utf-8")
    draft = to_draft(tex_path.read_text(encoding="utf-8"), previous)
    header = (
        f"# DRAFT built from {tex_path.name}. Review every line, fill each TODO, then save as profile.yaml.\n"
        "# Only true facts. Bullets here are what the truth gate checks resumes and answers against.\n"
    )
    text = yaml.safe_dump(draft, sort_keys=False, allow_unicode=True, width=110)
    draft_path.write_text(header + text, encoding="utf-8")
    return draft_path, text.count(TODO), backup
