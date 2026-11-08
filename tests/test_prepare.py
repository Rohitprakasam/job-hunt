import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent import gates
from agent.discover.base import RawJob, upsert
from agent.prepare import answers, runner, tailor, truth

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
JOB = {
    "title": "Security Analyst",
    "company": "Acme",
    "description": "Requirements: Linux, SIEM, Kubernetes. AWS GuardDuty.",
}


# ---------------------------------------------------------------- answers


def test_seed_and_lookup(conn, profile):
    assert answers.seed(conn, profile) > 10
    assert answers.lookup(conn, "First Name *").value == profile.contact.first_name
    assert answers.lookup(conn, "Email address").value == profile.contact.email
    assert answers.lookup(conn, "Are you legally authorized to work in India?", ["Yes", "No"]).value == "Yes"
    assert answers.lookup(conn, "Favourite colour?") is None


def test_hand_answers_survive_reseed(conn, profile):
    answers.upsert(conn, "Phone number", "+911111111111", "factual", approved=True)
    answers.seed(conn, profile)
    assert answers.lookup(conn, "Phone number").value == "+911111111111"


def test_legal_never_from_unapproved_or_llm(conn, profile):
    answers.upsert(conn, "Do you have a disability?", "No", "legal", approved=False)
    assert answers.lookup(conn, "Do you have a disability?") is None
    assert (
        answers.derive(object(), profile, "Will you require visa sponsorship?") is None
    )  # never asks the LLM


def test_match_option():
    assert answers.match_option("yes", ["Yes, I am", "No"]) == "Yes, I am"
    assert answers.match_option("Bengaluru", ["Chennai", "Bengaluru"]) == "Bengaluru"
    assert answers.match_option("Mars", ["Chennai"]) is None


def test_pending_questions(conn):
    answers.record_pending(conn, "Why us?", None)
    answers.record_pending(conn, "Why us? *", None)
    row = conn.execute("SELECT * FROM pending_questions").fetchone()
    assert row["seen_count"] == 2
    answers.upsert(conn, "Why us?", "Because.", "factual", approved=True)
    assert conn.execute("SELECT count(*) FROM pending_questions").fetchone()[0] == 0


# ---------------------------------------------------------------- tailoring + truth gate


def test_deterministic_tailoring_is_truthful(profile):
    t = tailor.deterministic(profile, JOB)
    assert t.method == "deterministic"
    assert truth.check(profile, t, JOB["description"]) == []


def _first_bullet(t):
    return t.experience[0].bullets[0]


def test_truth_gate_catches_planted_skill(profile):
    t = tailor.deterministic(profile, JOB)
    b = _first_bullet(t)
    b.text = b.text + " Deployed workloads on Kubernetes."
    problems = truth.check(profile, t, JOB["description"])
    assert any("kubernetes" in p for p in problems)


def test_truth_gate_catches_changed_number_and_copied_term(profile):
    t = tailor.deterministic(profile, JOB)
    b = _first_bullet(t)
    b.text = b.text.replace("99.9", "99.99") + " Used GuardDuty."
    problems = " ".join(truth.check(profile, t, JOB["description"]))
    assert "99.99" in problems and "GuardDuty" in problems


def test_truth_gate_catches_unknown_ids_and_skills(profile):
    t = tailor.deterministic(profile, JOB)
    t.experience[0].bullets.append(tailor.TBullet(id="made-up-01", text="Led a team"))
    t.skills.append("Terraform")
    problems = truth.check(profile, t)
    assert "unknown bullet id made-up-01" in problems and "skill not in profile: Terraform" in problems


# ---------------------------------------------------------------- gates


def test_fit_gate(settings):
    assert gates.fit_gate(80, None, settings.apply).ok
    assert not gates.fit_gate(60, None, settings.apply).ok
    assert not gates.fit_gate(95, "too_senior", settings.apply).ok


def test_answer_gate():
    ok = [gates.FieldPlan("Email", True, "email", "a@b.c", "bank")]
    assert gates.answer_gate(ok).ok
    missing = ok + [gates.FieldPlan("Why us?", True, "textarea", None, None)]
    assert not gates.answer_gate(missing).ok
    legal_llm = [gates.FieldPlan("Need sponsorship?", True, "select", "No", "llm", category="legal")]
    assert not gates.answer_gate(legal_llm).ok


def test_form_gate():
    assert gates.form_gate({"Email": "a@b.c"}, {"Email": " A@b.c "}, []).ok
    assert not gates.form_gate({"Email": "a@b.c"}, {"Email": ""}, []).ok
    assert not gates.form_gate({"Email": "a@b.c"}, {"Email": "a@b.c"}, ["Email is invalid"]).ok


def test_proof_gate(tmp_path):
    shot = tmp_path / "s.png"
    assert not gates.proof_gate(shot, True).ok
    shot.write_bytes(b"png")
    assert gates.proof_gate(shot, True).ok and not gates.proof_gate(shot, False).ok


def _submitted(conn, company, when, outcome="submitted"):
    job_id, _ = upsert(conn, RawJob("greenhouse", f"{company}{when}", company, f"Role {when}"), NOW)
    cur = conn.execute(
        "INSERT INTO applications(job_id, status, origin, submitted_at) VALUES (?, 'submitted', 'agent', ?)",
        (job_id, when),
    )
    conn.execute(
        "INSERT INTO apply_attempts(application_id, adapter, mode, outcome, started_at) VALUES (?, 'greenhouse',"
        " 'live', ?, ?)",
        (cur.lastrowid, outcome, when),
    )


def test_volume_gate(conn, settings):
    recent = "2026-10-28T12:00:00.000Z"
    _submitted(conn, "Acme", recent)
    assert not gates.volume_gate(conn, "ACME Inc", settings.apply, NOW).ok  # same company within 30 days
    assert gates.volume_gate(conn, "Beta", settings.apply, NOW).ok
    capped = settings.apply.model_copy(update={"daily_cap": 1})
    assert "daily cap" in gates.volume_gate(conn, "Beta", capped, NOW).detail
    for i in range(3):
        _submitted(conn, f"Fail{i}", recent, outcome="failed")
    assert (
        "failed in a row" in gates.volume_gate(conn, "Gamma", settings.apply, NOW + timedelta(days=2)).detail
    )


def test_block_gate():
    assert not gates.block_gate("Please verify you are human", [], False).ok
    assert not gates.block_gate("", ["https://www.google.com/recaptcha/api2/bframe"], False).ok
    assert not gates.block_gate("Apply", [], True).ok
    assert gates.block_gate("Apply for this job", [], False).ok


def test_gate_results_serialise():
    assert json.dumps(gates.as_json([gates.GateResult("fit", True, "x")]))
    assert Path  # keep import used


def test_rules_from_profile(profile):
    assert (
        answers.rule_answer(
            "Have you previously worked at or consulted for Example Corp?", profile, "Example Corp"
        )
        == "Yes"
    )
    assert answers.rule_answer("Have you previously worked for GitLab?", profile, "GitLab") == "No"
    assert answers.rule_answer("What is your current country of residence?", profile) == "India"
    assert answers.rule_answer("What's the name you'd prefer us to use?", profile) == "Testa"
    assert answers.rule_answer("Why do you want this job?", profile) is None


def test_legal_intent_needs_same_country(conn, profile):
    answers.seed(conn, profile)
    q = "Will you now or in the future require sponsorship for a visa to remain in your current country?"
    assert answers.lookup(conn, q, ["Yes", "No"]).value == "No"
    assert answers.lookup(conn, "Are you legally eligible to work in India?").value == "Yes"
    assert answers.lookup(conn, "Are you legally eligible to work in the United States?") is None


def test_cover_may_name_the_role_and_company(profile):
    t = tailor.deterministic(profile, JOB)
    title = "Security Analyst (VAPT / Kubernetes)"
    cover = f"I am applying for the {title} role at Acme. {t.summary}"
    assert not gates.truth_gate(profile, t, title, cover).ok
    assert gates.truth_gate(profile, t, title, cover, (title, "Acme")).ok
    assert not gates.truth_gate(profile, t, title, cover + " I know Kubernetes.", (title, "Acme")).ok


def test_more_profile_rules(conn, profile):
    answers.seed(conn, profile)
    visa = "Do you, now or in the future, require a work permit, visa, or additional right to work support?"
    assert answers.lookup(conn, visa, ["Yes", "No"]).value == "No"
    assert answers.rule_answer("In what city and state is your primary residence?", profile) == "Chennai"
    assert answers.rule_answer("Current Title", profile) is None  # the example internship has ended


def test_candidates_dedupes_company_and_skips_recent(conn, settings):
    # Company Alpha has 2 jobs (fit 90 and fit 80)
    conn.execute(
        "INSERT INTO jobs (id, source, external_id, title, company, url, apply_url, ats_type, first_seen_at, dedupe_key)"
        " VALUES (1, 'greenhouse', 'e1', 'Job A1', 'Company Alpha', 'u1', 'https://job-boards.greenhouse.io/embed/job_app?for=alpha&token=1', 'greenhouse', '2026-11-08T00:00:00Z', 'alpha|job1')"
    )
    conn.execute(
        "INSERT INTO scores (job_id, fit, priority, scored_at) VALUES (1, 90.0, 90.0, '2026-11-08T00:00:00Z')"
    )

    conn.execute(
        "INSERT INTO jobs (id, source, external_id, title, company, url, apply_url, ats_type, first_seen_at, dedupe_key)"
        " VALUES (2, 'greenhouse', 'e2', 'Job A2', 'Company Alpha', 'u2', 'https://job-boards.greenhouse.io/embed/job_app?for=alpha&token=2', 'greenhouse', '2026-11-08T00:00:00Z', 'alpha|job2')"
    )
    conn.execute(
        "INSERT INTO scores (job_id, fit, priority, scored_at) VALUES (2, 80.0, 80.0, '2026-11-08T00:00:00Z')"
    )

    # Company Beta has 1 job (fit 85)
    conn.execute(
        "INSERT INTO jobs (id, source, external_id, title, company, url, apply_url, ats_type, first_seen_at, dedupe_key)"
        " VALUES (3, 'greenhouse', 'e3', 'Job B1', 'Company Beta', 'u3', 'https://job-boards.greenhouse.io/embed/job_app?for=beta&token=3', 'greenhouse', '2026-11-08T00:00:00Z', 'beta|job1')"
    )
    conn.execute(
        "INSERT INTO scores (job_id, fit, priority, scored_at) VALUES (3, 85.0, 85.0, '2026-11-08T00:00:00Z')"
    )

    # Company Gamma had an application submitted 3 days ago
    conn.execute(
        "INSERT INTO jobs (id, source, external_id, title, company, url, apply_url, ats_type, first_seen_at, dedupe_key)"
        " VALUES (4, 'greenhouse', 'e4', 'Job C1', 'Company Gamma', 'u4', 'https://job-boards.greenhouse.io/embed/job_app?for=gamma&token=4', 'greenhouse', '2026-11-08T00:00:00Z', 'gamma|job1')"
    )
    conn.execute(
        "INSERT INTO scores (job_id, fit, priority, scored_at) VALUES (4, 95.0, 95.0, '2026-11-08T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO applications (job_id, status, origin, submitted_at)"
        " VALUES (4, 'submitted', 'agent', '2026-11-05T00:00:00Z')"
    )

    cands = runner.candidates(conn, settings, limit=10)
    cand_ids = [c["id"] for c in cands]
    assert 4 not in cand_ids
    assert 1 in cand_ids
    assert 2 not in cand_ids
    assert 3 in cand_ids
