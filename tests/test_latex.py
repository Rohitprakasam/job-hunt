import json
from types import SimpleNamespace

import pytest

from agent import config
from agent.prepare import latex
from tests.conftest import EXAMPLES

TEMPLATE = (EXAMPLES / "resume.example.tex").read_text(encoding="utf-8")
BACKEND = {
    "title": "Backend Engineer II (Python), Payments",
    "company": "Zeta & Co",
    "description": "Requirements: Python, Postgres, Docker, Kubernetes, REST APIs, CI/CD, AWS. Nice: ReactJS, Burp Suite.",
}


@pytest.fixture
def rs():
    return config.ResumeSettings(engine="latex", llm_summary=False)


def test_clean_title():
    assert latex.clean_title("Backend Engineer II (Python), Payments") == "Backend Engineer"
    assert latex.clean_title("Senior Security Analyst - SOC") == "Security Analyst"
    assert latex.clean_title("Engineer, Site Reliability | Remote") == "Engineer"
    assert latex.clean_title("((()))") is None


def test_tailor_changes_only_words(profile, rs):
    t = latex.tailor(TEMPLATE, BACKEND, profile, rs)
    assert "selectfont Backend Engineer $|$ Python (FastAPI) $|$ Software Engineer" in t.tex
    assert t.tex.count("$|$", t.tex.find("selectfont Backend")) >= 4
    assert r"Seeking a Backend Engineer role at Zeta \& Co." in t.tex
    assert r"\textbf{Databases:} Postgres, MongoDB, SQL" in t.tex
    assert "REST APIs, FastAPI" in t.tex and "ReactJS" in t.tex and "Burp Suite" in t.tex
    assert "kubernetes" in t.missing and "postgresql" in t.matched
    # Experience is never touched, and the headline keeps its number of parts.
    assert "Migrated a legacy PHP portal to React.js" in t.tex
    original_parts = latex.HEADLINE_RE.search(TEMPLATE).group(2).count("$|$")
    assert latex.HEADLINE_RE.search(t.tex).group(2).count("$|$") == original_parts


def test_tailor_without_title_change(profile):
    t = latex.tailor(
        TEMPLATE, BACKEND, profile, config.ResumeSettings(headline_from_job=False, llm_summary=False)
    )
    assert "Seeking a Software Engineer role." in t.tex


def test_split_top_keeps_parentheses():
    assert latex.split_top("Docker, AWS (CloudFront, WAF), Vercel") == [
        "Docker",
        "AWS (CloudFront, WAF)",
        "Vercel",
    ]


def test_plain_has_no_markup():
    assert latex.plain(r"\textbf{React.js} \& 99.9\% $|$ x~y") == "React.js & 99.9% | x y"


@pytest.mark.parametrize(
    "new, problem",
    [
        (
            r"Graduate. Building \textbf{Kubernetes} and FastAPI with 99.9\%. Seeking a Software Engineer role.",
            "skills not in resume",
        ),
        (
            r"Graduate with 7 years. Kept servers at 99.9\% uptime. Seeking a Software Engineer role.",
            "numbers",
        ),
        (r"Graduate \emph{x}. Kept 5+ servers at 99.9\%. Seeking a Software Engineer role.", "commands"),
        (r"Graduate & intern. Kept 5+ servers at 99.9\%. Seeking a Software Engineer role.", "unescaped"),
        (r"Graduate. Kept 5+ servers at 99.9\%.", "Seeking"),
    ],
)
def test_check_summary_rejects(profile, new, problem):
    original = latex.SUMMARY_RE.search(TEMPLATE).group(2).strip()
    problems = latex.check_summary(original, new, TEMPLATE, BACKEND["description"], profile)
    assert any(problem in p for p in problems), problems


class FakeClient:
    def __init__(self, text):
        self.text = text

    def complete(self, *args, **kwargs):
        return SimpleNamespace(data=latex.Summary(summary=self.text))


def test_llm_summary_kept_only_when_true(profile):
    original = latex.SUMMARY_RE.search(TEMPLATE).group(2).strip()
    good = original.replace("Building a", "Developing a")
    tex, problems, note = latex.llm_summary(FakeClient(good), TEMPLATE, BACKEND, profile)
    assert problems == [] and note and "Developing a" in tex
    bad = original.replace("Building a", "Building a Kubernetes-based")
    tex, problems, note = latex.llm_summary(FakeClient(bad), TEMPLATE, BACKEND, profile)
    assert problems and tex == TEMPLATE and note is None


def test_build_reverts_when_too_long(profile, rs, tmp_path, monkeypatch):
    template = tmp_path / "resume.tex"
    template.write_text(TEMPLATE, encoding="utf-8")
    compiled = []

    def fake_compile(tex, out_dir, name, command=""):
        compiled.append(tex)
        pdf = out_dir / name
        pdf.write_bytes(b"%PDF")
        return pdf

    pages = iter([2, 1])
    monkeypatch.setattr(latex, "compile_pdf", fake_compile)
    monkeypatch.setattr(latex, "page_count", lambda pdf: next(pages))
    pdf, result = latex.build(profile, BACKEND, rs, template, tmp_path)
    assert compiled[-1] == TEMPLATE and "reverted" in result.changes[-1]
    notes = (tmp_path / "resume-changes.txt").read_text(encoding="utf-8")
    assert "ATS keywords matched" in notes and "kubernetes" in notes
    assert pdf.name == "Testa_Candidato_Q_Resume.pdf"


def test_missing_engine_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(latex, "find_engine", lambda command="": None)
    with pytest.raises(latex.LatexError, match="no LaTeX engine"):
        latex.compile_pdf(TEMPLATE, tmp_path, "r.pdf")


@pytest.mark.skipif(latex.find_engine() is None, reason="no LaTeX engine installed")
def test_real_compile(profile, rs, tmp_path):
    template = tmp_path / "resume.tex"
    template.write_text(TEMPLATE, encoding="utf-8")
    pdf, _ = latex.build(profile, BACKEND, rs, template, tmp_path)
    assert latex.page_count(pdf) == 1


def test_changes_file_is_json_free_text(profile, rs):
    assert json.dumps(latex.tailor(TEMPLATE, BACKEND, profile, rs).changes)


def test_prepare_stage_uses_latex(conn, settings, profile, tmp_path, monkeypatch):
    from agent.discover.base import RawJob, upsert
    from agent.prepare import runner

    monkeypatch.setenv("JOB_AGENT_CONFIG", str(tmp_path))
    (tmp_path / "resume.tex").write_text(TEMPLATE, encoding="utf-8")

    def fake_compile(tex, out, name, cmd=""):
        (out / name).write_bytes(b"%PDF")
        return out / name

    monkeypatch.setattr(latex, "compile_pdf", fake_compile)
    monkeypatch.setattr(latex, "page_count", lambda pdf: 1)
    job_id, _ = upsert(
        conn,
        RawJob("greenhouse", "1", "Zeta", "Backend Engineer", description=BACKEND["description"]),
        latex_now(),
    )
    job = conn.execute("SELECT j.*, 80.0 AS fit FROM jobs j WHERE id = ?", (job_id,)).fetchone()
    s = settings.model_copy(update={"resume": config.ResumeSettings(engine="latex", llm_summary=False)})
    app_id, ok = runner.prepare_one(conn, s, profile, None, job)
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert ok and row["resume_path"].endswith("Testa_Candidato_Q_Resume.pdf")
    assert any(g["gate"] == "ats" for g in json.loads(row["gate_results_json"]))


def latex_now():
    from datetime import UTC, datetime

    return datetime(2026, 10, 29, tzinfo=UTC)


def test_prepare_pauses_without_engine(conn, settings, profile, monkeypatch):
    from agent.prepare import runner

    monkeypatch.setattr(latex, "find_engine", lambda command="": None)
    s = settings.model_copy(update={"resume": config.ResumeSettings(engine="latex")})
    result = runner.run_prepare(conn, s, profile)
    assert result.stopped == "no LaTeX engine"
    assert conn.execute("SELECT count(*) FROM applications").fetchone()[0] == 0
