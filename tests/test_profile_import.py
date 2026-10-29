import copy

import pytest
import yaml

from agent import config
from agent import profile_import as pi

PORTFOLIO = {
    "heroSection": {"fullName": "Testa Candidato Q"},
    "aboutMe": {"description": "Linux admin focused on security. 😊😊"},
    "contact": {
        "email": "testa.candidato@example.com",
        "phone": "+91-9000000001",
        "linkedin": "https://linkedin.com/in/testa-candidato-q-000000001/",
        "github": "https://github.com/TestaCandidato",
        "instagram": "https://www.instagram.com/testa__fake?igsh=abc",
    },
    "skills": [{"category": "System Administration", "items": ["Linux", "Bash"]}],
    "experience": [
        {
            "role": "System Administrator Intern",
            "organization": "Example Corp",
            "location": "Chennai",
            "duration": "Mar 2024 – Apr 2024",
            "highlights": ["Kept uptime at 99.9%.", "Automated backups."],
        }
    ],
    "education": [
        {
            "institution": "Example Institute",
            "degree": "B.E.",
            "duration": "Dec 2022 - May 2026",
            "score": "7.0",
        }
    ],
    "projects": [
        {"title": "Wi-Fi Assessment", "description": "Lab WPA2 review.", "tags": ["WiFi"], "link": "#"}
    ],
    "certifications": [{"title": "RHCSA", "url": "#", "type": "other"}],
}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Mar 2024 – Apr 2024", ("2024-03", "2024-04")),
        ("Dec 2022 - May 2026", ("2022-12", "2026-05")),
        ("Jun 2024 — Present", ("2024-06", None)),
        ("Jul 2023", ("2023-07", None)),
        (None, (None, None)),
    ],
)
def test_parse_duration(text, expected):
    assert pi.parse_duration(text) == expected


def test_mapping():
    draft = pi.to_draft(PORTFOLIO)
    contact = draft["contact"]
    assert (contact["first_name"], contact["last_name"]) == ("Testa", "Candidato")
    assert contact["name_variants"] == ["Testa Candidato", "Candidato Q"]
    assert contact["phone_e164"] == "+919000000001"
    assert [link["handle"] for link in contact["links"]] == [
        "testa-candidato-q-000000001",
        "TestaCandidato",
        "testa__fake",
    ]
    assert contact["links"][2]["on_resume"] is False
    exp = draft["experience"][0]
    assert (exp["id"], exp["start"], exp["end"]) == ("example-corp", "2024-03", "2024-04")
    assert [b["id"] for b in exp["bullets"]] == ["example-corp-01", "example-corp-02"]
    assert draft["summary"] == "Linux admin focused on security."
    assert draft["projects"][0]["url"] is None
    assert draft["graduation"]["date"] == "2026-05"


def test_draft_fails_validation_until_todos_filled(tmp_path, conn):
    target = tmp_path / "profile.draft.yaml"
    result = pi.write_draft(conn, PORTFOLIO, target)
    assert result.written and result.todos > 0
    with pytest.raises(config.ConfigError, match="TODO"):
        config.load_profile(target)


def test_filled_draft_validates(tmp_path):
    draft = pi.to_draft(PORTFOLIO)
    draft["contact"].update(city="Chennai", country="India")
    draft["work_authorization"]["countries"] = ["India"]
    draft["notice_period_days"] = 0
    draft["salary_expectation"].update(min=300000, target=400000)
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(draft), encoding="utf-8")
    config.load_profile(path)


def test_changed_source_needs_confirmation(tmp_path, conn):
    target = tmp_path / "profile.draft.yaml"
    pi.write_draft(conn, PORTFOLIO, target)
    assert pi.write_draft(conn, PORTFOLIO, target).written  # same data: fine
    changed = copy.deepcopy(PORTFOLIO)
    changed["contact"]["email"] = "attacker@example.org"
    before = target.read_text(encoding="utf-8")
    result = pi.write_draft(conn, changed, target)
    assert not result.written and result.changed_since_last
    assert target.read_text(encoding="utf-8") == before
    assert pi.write_draft(conn, changed, target, confirm_changed=True).written


def test_load_portfolio_ts(tmp_path):
    source = tmp_path / "portfolio.ts"
    body = '{"heroSection": {"fullName": "A {curly} \\"quoted\\" name"}, "skills": []}'
    source.write_text(f"export const portfolioData = {body};\n\nexport interface X {{ a: string; }}\n")
    assert pi.load_portfolio_ts(source)["heroSection"]["fullName"] == 'A {curly} "quoted" name'
