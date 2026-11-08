"""Resume PDF via RenderCV (YAML -> Typst -> PDF). Contact details are added here, locally."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from agent.config import Profile
from agent.prepare.tailor import Tailored

NETWORKS = {"linkedin": "LinkedIn", "github": "GitHub", "gitlab": "GitLab", "instagram": "Instagram"}


class RenderError(Exception):
    pass


def _date(value: str | None) -> str:
    return value or "present"


def rendercv_document(profile: Profile, tailored: Tailored) -> dict:
    c = profile.contact
    bullets = {b.id: b for b in profile.all_bullets()}
    social, website = [], None
    for link in c.links:
        if not link.on_resume:
            continue
        network = NETWORKS.get(link.label.lower())
        if network and link.handle:
            social.append({"network": network, "username": link.handle})
        elif website is None:
            website = link.url
    experience = []
    by_id = {e.id: e for e in profile.experience}
    for section in tailored.experience:
        e = by_id[section.id]
        experience.append(
            {
                "company": e.company,
                "position": e.title,
                "start_date": e.start,
                "end_date": _date(e.end),
                **({"location": e.location} if e.location else {}),
                "highlights": [b.text for b in section.bullets if b.id in bullets],
            }
        )
    projects = []
    p_by_id = {p.id: p for p in profile.projects}
    for section in tailored.projects:
        p = p_by_id.get(section.id)
        if p is None or not section.bullets:
            continue
        name = f"[{p.name}]({p.url})" if p.url else p.name
        projects.append({"name": name, "highlights": [b.text for b in section.bullets]})
    education = [
        {
            "institution": ed.institution,
            "area": ed.degree,
            **({"start_date": ed.start} if ed.start else {}),
            **({"end_date": ed.end} if ed.end else {}),
            **({"highlights": [ed.score]} if ed.score else {}),
        }
        for ed in profile.education
    ]
    groups = {}
    ordered = [s.lower() for s in tailored.skills]
    for label, items in profile.skills.items():
        chosen = sorted((s for s in items if s.lower() in ordered), key=lambda s: ordered.index(s.lower()))
        if chosen:
            groups[label] = chosen
    sections: dict[str, list] = {"summary": [tailored.summary]}
    if experience:
        sections["experience"] = experience
    if projects:
        sections["projects"] = projects
    sections["education"] = education
    if groups:
        sections["skills"] = [{"label": k, "details": ", ".join(v)} for k, v in groups.items()]
    if profile.certifications:
        sections["certifications"] = [c_.name for c_ in profile.certifications]
    cv = {
        "name": c.full_name,
        "email": c.email,
        "phone": c.phone_e164,
        "location": ", ".join(x for x in (c.city, c.country) if x),
        **({"website": website} if website else {}),
        **({"social_networks": social} if social else {}),
        "sections": sections,
    }
    design = {"theme": "classic", "header": {"connections": {"phone_number_format": "international"}}}
    return {"cv": cv, "design": design}


def render_pdf(profile: Profile, tailored: Tailored, out_dir: Path, timeout_s: int = 180) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    source = out_dir / "resume.yaml"
    source.write_text(
        yaml.safe_dump(rendercv_document(profile, tailored), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    pdf = out_dir / f"{profile.contact.full_name.replace(' ', '_')}_Resume.pdf"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "rendercv", "render", str(source), "--pdf-path", str(pdf),
             "--output-folder", str(out_dir / "rendercv"), "-nomd", "-nohtml", "-nopng"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s, env=env,
            cwd=out_dir,
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise RenderError("rendercv timed out") from exc
    if not pdf.exists():
        tail = (proc.stdout + proc.stderr)[-600:]
        raise RenderError(f"rendercv failed (exit {proc.returncode}): {tail}")
    return pdf
