import yaml

from agent import config, profile_tex
from agent.prepare import latex
from tests.conftest import EXAMPLES

TEX = (EXAMPLES / "resume.example.tex").read_text(encoding="utf-8")


def test_body_text_has_no_header_or_preamble():
    body = latex.body_text(TEX)
    assert body.startswith("Professional Summary")
    assert "example.com" not in body and "90000" not in body and "geometry" not in body


def test_draft_from_tex():
    draft = profile_tex.to_draft(TEX)
    c = draft["contact"]
    assert (c["city"], c["country"], c["phone_e164"], c["email"]) == (
        "Chennai", "India", "+919000000001", "testa.candidato@example.com",
    )  # fmt: skip
    exp = draft["experience"][0]
    assert (exp["company"], exp["title"], exp["start"], exp["end"]) == (
        "Example Corp",
        "Developer Intern",
        "2026-07",
        None,
    )
    assert exp["bullets"][0]["text"].startswith("Migrated a legacy PHP portal")
    assert draft["skills"]["Databases"] == ["PostgreSQL", "MongoDB", "SQL"]
    assert draft["skills"]["DevOps & Cloud"][3] == "AWS (CloudFront, WAF)"
    assert "Seeking a Software Engineer role." in draft["summary"]


def test_previous_draft_values_are_kept(tmp_path):
    tex = tmp_path / "resume.tex"
    tex.write_text(TEX, encoding="utf-8")
    draft = tmp_path / "profile.draft.yaml"
    draft.write_text(yaml.safe_dump({"contact": {"full_name": "Testa Candidato Q", "first_name": "Testa",
                                                 "last_name": "Candidato", "phone_e164": "TODO"},
                                     "notice_period_days": 0}), encoding="utf-8")  # fmt: skip
    path, todos, backup = profile_tex.write(tex, draft)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["notice_period_days"] == 0 and data["contact"]["first_name"] == "Testa"
    assert backup and backup.exists() and todos > 0


def test_filled_draft_validates(tmp_path):
    draft = profile_tex.to_draft(
        TEX, {"contact": {"full_name": "Testa Candidato Q", "first_name": "Testa", "last_name": "Candidato"}}
    )
    draft["work_authorization"] = {"countries": ["India"], "needs_sponsorship": False}
    draft["notice_period_days"] = 0
    draft["salary_expectation"] = {"currency": "INR", "min": 300000, "target": 400000}
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(draft), encoding="utf-8")
    profile = config.load_profile(path)
    assert profile.experience[0].end is None
