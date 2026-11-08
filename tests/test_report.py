import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from agent import cloud as cloud_mod
from agent import db, scheduler
from agent.backup import nightly
from agent.discover.base import RawJob, upsert
from agent.report import experiments, prep, refit, sanitize, weekly
from tests.conftest import EXAMPLES
from tests.test_cloud_whatsapp import cloud, mongo  # noqa: F401 (fixtures)

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)


def _app(
    conn, company, status="submitted", fit=75.0, title="Security Analyst", source="greenhouse", days_ago=3
):
    job_id, _ = upsert(conn, RawJob(source, f"{company}-{title}", company, title, url="https://x"), NOW)
    conn.execute(
        "INSERT OR IGNORE INTO scores(job_id, fit, semantic, skills, llm_rubric, seniority, freshness, gaps_json,"
        " scored_at, priority) VALUES (?, ?, 0.6, 0.5, 0.7, 1, 0.5, '[\"kubernetes\"]', ?, ?)",
        (job_id, fit, db.to_iso(NOW), fit),
    )
    cur = conn.execute(
        "INSERT INTO applications(job_id, status, origin, submitted_at) VALUES (?, ?, 'agent', ?)",
        (job_id, status, db.to_iso(NOW - timedelta(days=days_ago))),
    )
    return cur.lastrowid


# ---------------------------------------------------------------- sanitizer


def test_snapshot_has_only_allowed_data(conn):
    _app(conn, "Secret Company Pvt Ltd", status="interview")
    payload = sanitize.build(conn, NOW, uptime_s=60, apply_paused=False)
    text = json.dumps(payload)
    assert "Secret" not in text and "Security Analyst" not in text and "https://" not in text
    assert payload["funnel_30d"]["interview"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(company="Acme"),
        lambda p: p["worker"].update(note="hi"),
        lambda p: p["sources"].append({"name": "Acme Careers", "jobs": 1, "errors": 0}),
        lambda p: p["stages"].update(discover="Acme broke"),
        lambda p: p["counts_today"].update(found=-1),
        lambda p: p.update(sent_at="Acme Corp hiring"),
    ],
)
def test_sanitizer_rejects_anything_else(conn, mutate):
    payload = sanitize.build(conn, NOW, 0, False)
    mutate(payload)
    with pytest.raises((sanitize.SanitizeError, ValueError)):
        sanitize.validate(payload)


def test_snapshot_written_with_history(conn, cloud, mongo):  # noqa: F811
    payload = sanitize.build(conn, NOW, 0, False)
    assert cloud.write_snapshot(payload)
    docs = {d["_id"] for d in mongo["job_agent"]["snapshot"].find()}
    assert docs == {"latest", "2026-10-29"}
    schema = cloud_mod.snapshot_validator()["$jsonSchema"]
    assert schema["additionalProperties"] is False and "_id" in schema["required"]


# ---------------------------------------------------------------- experiments / refit / weekly


def test_wilson_and_compare(conn):
    low, high = experiments.wilson(5, 10)
    assert 0.2 < low < 0.5 < high < 0.8
    assert experiments.assign_variant(conn, "summary") in ("A", "B")
    arms, verdict = experiments.compare(conn)
    assert arms == [] and "not enough data" in verdict


def test_refit_needs_data_then_fits(conn, tmp_path):
    assert refit.refit(conn, tmp_path / "w.json") == (None, 0)
    data = [([0.9, 0.9, 0.9, 1, 0.5], 1)] * 30 + [([0.1, 0.2, 0.3, 1, 0.5], 0)] * 30
    weights = refit.to_weights(refit.fit_logistic(data, steps=500))
    total = sum(weights.model_dump().values())
    assert total == pytest.approx(1, abs=0.01) and weights.seniority <= weights.semantic


def test_weekly_review(conn):
    _app(conn, "Acme", status="interview", source="greenhouse")
    _app(conn, "Beta", status="rejected", source="linkedin")
    _app(conn, "Gamma", status="submitted", source="linkedin")
    md, text = weekly.build(conn, NOW)
    assert "| greenhouse | 1 |" in md and "kubernetes" in md
    assert len(text.splitlines()) == 6 and "Acme" not in text


# ---------------------------------------------------------------- prep + referrals


def test_prep_pack_without_llm(conn, profile):
    app_id = _app(conn, "Acme", status="interview")
    assert prep.pending_packs(conn) == [app_id]
    path = prep.write_pack(conn, None, profile, app_id)
    assert path.exists() and "Interview prep" in path.read_text(encoding="utf-8")
    assert prep.pending_packs(conn) == []


def test_referrals(conn, tmp_path):
    _app(conn, "Acme Inc", fit=80)
    contacts = tmp_path / "contacts.csv"
    contacts.write_text("name,company,relation,link\nPriya S,ACME,college senior,\n", encoding="utf-8")
    path = prep.referral_drafts(conn, contacts)
    assert path and "Hi Priya" in path.read_text(encoding="utf-8")
    assert prep.referral_drafts(conn, tmp_path / "missing.csv") is None


# ---------------------------------------------------------------- backup + console + scheduler


def test_backup_keeps_n(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_CONFIG", str(tmp_path))
    (tmp_path / "profile.yaml").write_text("x", encoding="utf-8")
    for i in range(3):
        db_copy, archive = nightly(conn, keep=2, now=NOW + timedelta(seconds=i))
    assert archive.exists() and db_copy.exists()
    assert len(list(archive.parent.glob("config-*.zip"))) == 2


def test_console_pages(conn, tmp_path):
    from fastapi.testclient import TestClient

    from agent.console import create_app

    app_id = _app(conn, "Acme")
    conn.execute("UPDATE jobs SET apply_url = 'javascript:alert(1)'")
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    client = TestClient(create_app(__import__("pathlib").Path(path)))
    assert client.get("/").status_code == 200
    page = client.get(f"/applications/{app_id}").text
    assert "javascript:" not in page
    assert client.get("/jobs").status_code == 200 and client.get("/questions").status_code == 200
    assert client.get(f"/files/{app_id}/..%2F..%2Fsecret").status_code == 404


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    folder = tmp_path / "config"
    folder.mkdir()
    for name in ("settings", "search", "companies", "profile"):
        shutil.copy(EXAMPLES / f"{name}.example.yaml", folder / f"{name}.yaml")
    monkeypatch.setenv("JOB_AGENT_CONFIG", str(folder))
    return folder


def test_scheduler_stages_run_offline(config_dir, monkeypatch):
    monkeypatch.setattr("agent.discover.ats.fetch_company", lambda company, http: [])
    monkeypatch.setattr("agent.discover.jobspy_source._scrape", lambda **kw: [])
    for stage in (
        "discover",
        "score",
        "urgent",
        "digest",
        "prepare-apply",
        "heartbeat",
        "flush",
        "backup",
        "weekly",
    ):
        outcome = scheduler.run_stage(stage)
        assert not outcome.startswith("error"), (stage, outcome)
