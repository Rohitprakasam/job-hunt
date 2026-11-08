"""Your own LaTeX resume (config/resume.tex), lightly tailored per job and compiled to PDF.

Only small, truthful word changes, never new facts:
- headline: the job's title first, the other headline parts ordered by relevance;
- summary: "Seeking a <job title> role at <company>." and, optionally, a Gemini rewording toward the
  job's own terms that must pass a truth check (same numbers, no skill or term not already in the
  resume, same LaTeX markup, similar length) or it is discarded;
- skills: each line reordered so the job's skills come first, and spelled the way the job spells
  them when they are the same skill (PostgreSQL -> Postgres, React.js -> ReactJS);
- skill lines reordered so the most relevant category comes first.
Experience, projects, education, awards and the layout are never touched.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from agent.config import Profile, ResumeSettings
from agent.match import skills as skill_lib

log = logging.getLogger(__name__)

HEADLINE_RE = re.compile(r"(\\noindent\{\\itshape[^\n]*?\\selectfont[ \t]+)([^\n]*?)(\}[ \t]*)$", re.M)
SECTION_RE = r"\\sectionhead(?:\[[^\]]*\])?\{{{name}\}}\s*\\begin\{{adjustwidth\}}\{{[^}}]*\}}\{{[^}}]*\}}\s*(?:\\small\s*)?"
SUMMARY_RE = re.compile(
    f"({SECTION_RE.format(name='Professional Summary')})(.*?)(\\s*\\\\end\\{{adjustwidth\\}})", re.S
)
SKILLS_RE = re.compile(
    f"({SECTION_RE.format(name='Technical Skills')})(.*?)(\\\\end\\{{adjustwidth\\}})", re.S
)
SKILL_LINE_RE = re.compile(r"^([ \t]*\\item[ \t]+\\textbf\{([^}]*)\}[ \t]*)(.+?)[ \t]*$", re.M)
SEEKING_RE = re.compile(r"Seeking an? ([^.\n]{2,80}?) role(?: at [^.\n]{1,80})?\.")
SEP = r" $|$ "

# Same skill, different spellings (resume spelling <-> job spelling).
VARIANTS = [
    {"postgresql", "postgres"},
    {"react.js", "reactjs", "react"},
    {"next.js", "nextjs"},
    {"node.js", "nodejs"},
    {"javascript", "js"},
    {"typescript", "ts"},
    {"kubernetes", "k8s"},
    {"ci/cd", "ci cd", "cicd"},
    {"restful apis", "rest apis", "rest api", "restful api", "restful services"},
    {"spring boot", "springboot"},
    {"tailwind css", "tailwind", "tailwindcss"},
    {"mongodb", "mongo db"},
    {"github", "git hub"},
    {"linux administration", "linux system administration", "linux admin"},
    {"aws", "amazon web services"},
    {"gcp", "google cloud"},
    {"llm", "large language models", "llms"},
]
_LATEX_SPECIAL = {
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": "-",
    "^": "",
    "\\": "/",
}
_SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|manager|director|head|iii|ii|iv|i)\b", re.I)


class LatexError(Exception):
    pass


def escape(text: str) -> str:
    return "".join(_LATEX_SPECIAL.get(ch, ch) for ch in text)


def plain(tex: str) -> str:
    """Readable text from LaTeX: commands dropped, arguments kept."""
    text = re.sub(r"(?<!\\)%[^\n]*", "", tex)
    text = (
        text.replace(r"$|$", "|")
        .replace(r"\&", "&")
        .replace(r"\%", "%")
        .replace(r"\#", "#")
        .replace(r"\_", "_")
    )
    text = text.replace("\\ ", " ").replace("~", " ").replace("--", "-")
    text = re.sub(r"\\\\(\[[^\]]*\])?", " ", text)
    text = re.sub(r"\\href\{[^}]*\}", "", text)
    # Layout commands: drop them with their arguments (sizes, colours, environments).
    text = re.sub(r"\\(begin|end)\{[^}]*\}(\{[^}]*\})*", " ", text)
    text = re.sub(r"\\(vspace|hspace|fontsize|color|Needspace|setlength)\*?(\{[^}]*\})+", " ", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", " ", text)
    text = re.sub(r"[{}]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def first_section(tex: str) -> int:
    """Offset of the first section heading inside the document (the preamble defines the macro too)."""
    doc = max(0, tex.find(r"\begin{document}"))
    match = re.compile(r"\\sectionhead\s*[\[{]").search(tex, doc)
    return match.start() if match else doc


def body_text(tex: str) -> str:
    """Resume content without the preamble and header block (no contact details)."""
    return plain(tex[first_section(tex) :])


# ---------------------------------------------------------------- helpers


def split_top(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` outside parentheses/braces: 'AWS (CloudFront, WAF), Vercel' -> 2 parts."""
    parts, depth, buf = [], 0, ""
    for ch in text:
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _in(term: str, text_low: str) -> bool:
    return bool(re.search(rf"(?<![\w+#.]){re.escape(term.lower())}(?![\w+#])", text_low))


def job_terms(job_text: str, profile: Profile | None) -> set[str]:
    vocab = skill_lib.vocabulary(profile) if profile else set()
    return skill_lib.find_skills(job_text, vocab) if vocab else set()


def relevance(fragment: str, job_low: str, terms: set[str]) -> int:
    text = plain(fragment).lower()
    score = sum(2 for t in terms if _in(t, text))
    score += sum(1 for w in re.findall(r"[a-z][a-z0-9+#.]{3,}", text) if _in(w, job_low))
    return score


def job_spelling(skill_plain: str, job_text: str) -> str | None:
    """The job's spelling of the same skill, when it differs from ours and ours is absent from the job."""
    low = skill_plain.lower().strip()
    job_low = job_text.lower()
    if _in(low, job_low):
        # Same word: copy the job's capitalisation ("burp suite" -> "Burp Suite").
        match = re.search(rf"(?<![\w+#.]){re.escape(low)}(?![\w+#])", job_text, re.I)
        return match.group(0) if match and match.group(0) != skill_plain.strip() else None
    for group in VARIANTS:
        if low in group:
            for variant in sorted(group - {low}, key=len, reverse=True):
                match = re.search(rf"(?<![\w+#.]){re.escape(variant)}(?![\w+#])", job_text, re.I)
                if match:
                    return match.group(0)
    return None


def clean_title(title: str) -> str | None:
    """'Software Engineer II (Backend), Payments' -> 'Software Engineer'. None if nothing usable."""
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title)
    t = re.split(r"\s[-–—|/,]\s|,|:", t)[0]
    t = _SENIOR.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" -,")
    if not t or len(t) > 50 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 .&+#-]*", t):
        return None
    return t


# ---------------------------------------------------------------- edits


@dataclass
class Tailoring:
    tex: str
    changes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # truth-check failures of discarded LLM edits
    matched: list[str] = field(default_factory=list)  # job skills present in the final resume
    missing: list[str] = field(default_factory=list)  # job skills the resume does not have


def edit_headline(tex: str, title: str | None, job_low: str, terms: set[str]) -> tuple[str, str | None]:
    match = HEADLINE_RE.search(tex)
    if not match:
        return tex, None
    parts = [p.strip() for p in match.group(2).split("$|$")]
    first, rest = parts[0], parts[1:]
    keep = len(rest)
    if title and plain(first).lower() != title.lower():
        rest = [first, *rest]
        first = escape(title)
    rest = sorted(rest, key=lambda p: -relevance(p, job_low, terms))  # stable: ties keep your order
    new = SEP.join([first, *rest[:keep]])  # same number of parts: the line keeps its length
    if new == match.group(2):
        return tex, None
    return tex[: match.start(2)] + new + tex[match.end(2) :], f"headline: {plain(new)}"


def edit_seeking(tex: str, title: str | None, company: str) -> tuple[str, str | None]:
    summary = SUMMARY_RE.search(tex)
    if not summary or not title:
        return tex, None
    block = summary.group(2)
    match = SEEKING_RE.search(block)
    if not match:
        return tex, None
    article = "an" if title[0].lower() in "aeiou" else "a"
    sentence = f"Seeking {article} {escape(title)} role at {escape(company)}."
    block = block[: match.start()] + sentence + block[match.end() :]
    return tex[: summary.start(2)] + block + tex[summary.end(2) :], f"summary: {sentence}"


def edit_skills(tex: str, job_text: str, terms: set[str]) -> tuple[str, list[str]]:
    section = SKILLS_RE.search(tex)
    if not section:
        return tex, []
    job_low = job_text.lower()
    changes: list[str] = []
    lines = []
    for line in SKILL_LINE_RE.finditer(section.group(2)):
        prefix, label, items_tex = line.group(1), line.group(2), line.group(3)
        items = split_top(items_tex)
        renamed = []
        for item in items:
            spelling = job_spelling(plain(item), job_text)
            if spelling and "\\" not in item:
                changes.append(f"skills: {plain(item)} -> {spelling}")
                renamed.append(escape(spelling))
            else:
                renamed.append(item)
        ordered = sorted(renamed, key=lambda i: -relevance(i, job_low, terms))
        lines.append(
            (relevance(items_tex, job_low, terms), prefix + ", ".join(ordered), label, ordered != renamed)
        )
    if not lines:
        return tex, []
    for _, _, label, moved in lines:
        if moved:
            changes.append(f"skills: reordered {plain(label).rstrip(':')}")
    ranked = sorted(range(len(lines)), key=lambda i: -lines[i][0])
    if ranked != list(range(len(lines))):
        changes.append("skills: most relevant categories first")
    body = section.group(2)
    originals = list(SKILL_LINE_RE.finditer(body))
    new_body = body[: originals[0].start()]
    for slot, original in enumerate(originals):
        new_body += lines[ranked[slot]][1]
        end = originals[slot + 1].start() if slot + 1 < len(originals) else len(body)
        new_body += body[original.end() : end]
    return tex[: section.start(2)] + new_body + tex[section.end(2) :], changes


# ---------------------------------------------------------------- LLM summary (truth-checked)


class Summary(BaseModel):
    summary: str = Field(max_length=2000)


SUMMARY_SYSTEM = (
    "You lightly edit the LaTeX 'Professional Summary' of a resume for one job. Change only a few words so "
    "it uses the job posting's own terms where they mean exactly what is already written (synonyms, word "
    "order, emphasis with \\textbf{}). Strict rules: keep every fact and number; never add a skill, tool, "
    "employer, title, number or claim that is not in RESUME; keep the LaTeX valid (escape & as \\&, % as "
    "\\%); only use the \\textbf{} command; keep the same length (within 10%); keep the final 'Seeking ...' "
    "sentence exactly. Reply with JSON only."
)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
_TERM_RE = re.compile(
    r"\b(?:[A-Z]{2,}[A-Za-z0-9+#]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*|[A-Za-z]+[0-9+#]+[A-Za-z0-9+#]*)\b"
)
_ALLOWED_CMDS = {"textbf", "&", "%", "$", "#", "_"}


def check_summary(
    original_tex: str, new_tex: str, resume_tex: str, job_text: str, profile: Profile | None
) -> list[str]:
    problems = []
    old, new, whole = plain(original_tex), plain(new_tex), body_text(resume_tex)
    extra = {n.replace(",", "") for n in _NUMBER_RE.findall(new)} - {
        n.replace(",", "") for n in _NUMBER_RE.findall(old)
    }
    if extra:
        problems.append(f"numbers not in summary: {', '.join(sorted(extra))}")
    vocab = skill_lib.vocabulary(profile) if profile else set()
    if vocab:
        added = skill_lib.find_skills(new, vocab) - skill_lib.find_skills(whole, vocab)
        if added:
            problems.append(f"skills not in resume: {', '.join(sorted(added))}")
    whole_low = whole.lower()
    copied = {t for t in _TERM_RE.findall(job_text) if t.lower() not in whole_low and _in(t, new.lower())}
    if copied:
        problems.append(f"job terms not in resume: {', '.join(sorted(copied))}")
    if new_tex.count("{") != new_tex.count("}"):
        problems.append("unbalanced braces")
    cmds = set(re.findall(r"\\([a-zA-Z]+|.)", new_tex))
    if cmds - _ALLOWED_CMDS:
        problems.append(f"LaTeX commands not allowed: {', '.join(sorted(cmds - _ALLOWED_CMDS))}")
    if re.search(r"(?<!\\)[&%#_$]", new_tex.replace("$|$", "")):
        problems.append("unescaped LaTeX special character")
    if not 0.85 <= len(new) / max(1, len(old)) <= 1.15:
        problems.append("length changed too much")
    keep = SEEKING_RE.search(original_tex)
    if keep and keep.group(0) not in new_tex:
        problems.append("'Seeking' sentence changed")
    return problems


def llm_summary(
    client, tex: str, job, profile: Profile | None, run_id=None
) -> tuple[str, list[str], str | None]:
    """Returns (tex, problems, change note). On any problem the original tex is returned."""
    summary = SUMMARY_RE.search(tex)
    if not summary or client is None:
        return tex, [], None
    original = summary.group(2).strip()
    content = (
        f"RESUME\n{body_text(tex)[:6000]}\n\nSUMMARY (LaTeX, edit this)\n{original}\n\n"
        f"JOB: {job['title']} at {job['company']}\n{(job['description'] or '')[:5000]}"
    )
    try:
        result = client.complete(
            "tailor_summary", "fast",
            [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": content}],
            max_tokens=4000, json_schema=Summary, run_id=run_id,
        )  # fmt: skip
    except Exception as exc:
        log.warning("summary rewording skipped: %s", type(exc).__name__)
        return tex, [], None
    if result.data is None:
        return tex, [], None
    new = result.data.summary.strip()  # type: ignore[union-attr]
    problems = check_summary(original, new, tex, f"{job['title']}\n{job['description'] or ''}", profile)
    if problems:
        return tex, problems, None
    if plain(new) == plain(original):
        return tex, [], None
    return (
        tex[: summary.start(2)] + new + tex[summary.end(2) :],
        [],
        "summary: reworded toward the job's terms",
    )


def tailor(
    tex: str,
    job,
    profile: Profile | None,
    settings: ResumeSettings,
    client=None,
    run_id=None,
    summary_override: str | None = None,
) -> Tailoring:
    job_text = f"{job['title']}\n{job['description'] or ''}"
    terms = job_terms(job_text, profile)
    title = clean_title(job["title"]) if settings.headline_from_job else None
    out = Tailoring(tex)
    out.tex, note = edit_headline(out.tex, title, job_text.lower(), terms)
    out.changes += [note] if note else []
    out.tex, note = edit_seeking(out.tex, title, job["company"])
    out.changes += [note] if note else []
    out.tex, notes = edit_skills(out.tex, job_text, terms)
    out.changes += notes
    if summary_override:
        summary = SUMMARY_RE.search(out.tex)
        if summary:
            out.tex = (
                out.tex[: summary.start(2)]
                + "\n"
                + summary_override.strip()
                + "\n"
                + out.tex[summary.end(2) :]
            )
            out.changes.append("summary: reworded toward the job's terms")
    elif settings.llm_summary:
        out.tex, out.problems, note = llm_summary(client, out.tex, job, profile, run_id)
        out.changes += [note] if note else []
    final = body_text(out.tex).lower()
    out.matched = sorted(t for t in terms if _in(t, final))
    out.missing = sorted(t for t in terms if not _in(t, final))
    return out


# ---------------------------------------------------------------- compile


ENGINES = ("tectonic", "xelatex", "lualatex", "pdflatex")
_EXTRA_DIRS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64",
    Path("C:/Program Files/MiKTeX/miktex/bin/x64"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "job-agent" / "tools" / "tectonic",
]


def find_engine(command: str = "") -> Path | None:
    if command:
        found = shutil.which(command)
        return Path(found) if found else (Path(command) if Path(command).exists() else None)
    for name in ENGINES:
        found = shutil.which(name)
        if found:
            return Path(found)
    for folder in [*_EXTRA_DIRS, *sorted(Path("C:/texlive").glob("*/bin/windows"), reverse=True)]:
        for name in ENGINES:
            exe = folder / f"{name}.exe"
            if exe.exists():
                return exe
    return None


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf)).pages)


def compile_pdf(tex: str, out_dir: Path, pdf_name: str, command: str = "", timeout_s: int = 240) -> Path:
    engine = find_engine(command)
    if engine is None:
        raise LatexError("no LaTeX engine found (install Tectonic or MiKTeX, or set resume.latex_command)")
    build = out_dir / "latex"
    build.mkdir(parents=True, exist_ok=True)
    source = build / "resume.tex"
    source.write_text(tex, encoding="utf-8")
    if engine.stem.lower() == "tectonic":
        args = [str(engine), "--outdir", str(build), str(source)]
    else:
        args = [
            str(engine),
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={build}",
            str(source),
        ]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=build,
        )
    except subprocess.TimeoutExpired as exc:
        raise LatexError("LaTeX timed out") from exc
    built = build / "resume.pdf"
    if proc.returncode != 0 or not built.exists():
        raise LatexError(
            f"{engine.stem} failed (exit {proc.returncode}): {(proc.stdout + proc.stderr)[-500:]}"
        )
    target = out_dir / pdf_name
    shutil.copyfile(built, target)
    return target


def build(
    profile: Profile,
    job,
    settings: ResumeSettings,
    template: Path,
    out_dir: Path,
    client=None,
    run_id=None,
    summary_override: str | None = None,
    pdf_name: str | None = None,
) -> tuple[Path, Tailoring]:
    """Tailor + compile. If the tailored version runs over max_pages, retry with the plain template."""
    if not template.exists():
        raise LatexError(f"template not found: {template}")
    original = template.read_text(encoding="utf-8")
    result = tailor(original, job, profile, settings, client, run_id, summary_override=summary_override)
    (out_dir / "resume-changes.txt").write_text(
        "\n".join(result.changes or ["(no changes)"])
        + f"\n\nATS keywords matched: {', '.join(result.matched) or '-'}"
        + f"\nIn the job but not in your resume: {', '.join(result.missing) or '-'}\n",
        encoding="utf-8",
    )
    name = pdf_name or settings.pdf_name or f"{profile.contact.full_name.replace(' ', '_')}_Resume.pdf"
    pdf = compile_pdf(result.tex, out_dir, name, settings.latex_command)
    if page_count(pdf) > settings.max_pages and result.tex != original:
        log.warning("tailored resume ran over %s page(s); using the template as written", settings.max_pages)
        result.changes.append(f"reverted: tailored version was longer than {settings.max_pages} page(s)")
        result.tex = original
        pdf = compile_pdf(original, out_dir, name, settings.latex_command)
    return pdf, result
