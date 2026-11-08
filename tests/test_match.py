import json
from datetime import UTC, datetime, timedelta

import pytest

from agent import config, db
from agent.discover.base import RawJob, upsert
from agent.match import filters, score, similarity, skills
from tests.conftest import EXAMPLES
from tests.test_llm import FakeProvider

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)


@pytest.fixture
def search():
    return config.load_search(EXAMPLES / "search.example.yaml")


def _job(
    conn, title, *, company="Acme", location="Chennai", description="", posted=None, source="greenhouse"
):
    job_id, _ = upsert(
        conn,
        RawJob(source, title, company, title, apply_url="https://x/" + title.replace(" ", "-"),
               location=location, description=description, posted_at=posted or NOW - timedelta(hours=2)),
        NOW,
    )  # fmt: skip
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


@pytest.mark.parametrize(
    "title, kwargs, reason",
    [
        ("Security Analyst", {}, None),
        ("Systems Administrator", {}, None),
        ("Senior Security Analyst", {}, "too_senior"),
        ("Sr. Linux Administrator", {}, "too_senior"),
        ("Security Analyst", {"description": "Requires 5+ years of experience in SOC."}, "too_senior"),
        ("Security Analyst", {"description": "1-2 years experience preferred"}, None),
        ("Account Executive", {}, "irrelevant_title"),
        ("Security Analyst", {"location": "London, UK"}, "location"),
        ("Security Analyst", {"location": "Remote - India"}, None),
        ("Security Analyst", {"location": "Remote - USA"}, "location"),
        ("Security Analyst", {"location": "Remote"}, None),
        ("Security Analyst", {"location": "TN, India"}, None),
        ("Security Analyst", {"location": "India"}, None),
        ("Security Analyst", {"location": "MH, India"}, "location"),
        ("SDE3, Security Engineer", {}, "too_senior"),
        ("Security Analyst", {"posted": NOW - timedelta(days=45)}, "too_old"),
        ("Security Analyst", {"description": "This role is commission only"}, "blocked_keyword"),
    ],
)
def test_filters(conn, search, title, kwargs, reason):
    assert filters.check(_job(conn, title, **kwargs), search, NOW) == reason


def test_min_years():
    assert filters.min_years_required("3-5 years of experience") == 3
    assert filters.min_years_required("Founded 20 years ago") is None


def test_skill_coverage(profile):
    text = "Requirements\n- Linux\n- Bash\n- Kubernetes\nNice to have\n- Terraform"
    cov, found, gaps = skills.coverage(text, profile)
    assert "terraform" not in found
    assert found >= {"linux", "bash", "kubernetes"}
    assert "kubernetes" in gaps
    assert 0 < cov < 1
    assert skills.coverage("Nothing technical here.", profile)[0] == 0.5
    assert skills.find_skills("We use k8s and C++ daily", skills.vocabulary(profile)) >= {"kubernetes", "c++"}


def test_fit_formula_and_renormalisation(settings):
    w = settings.scoring.weights
    full = score.Parts(semantic=1, skills=1, seniority=1, freshness=1, llm=1)
    assert score.fit(full, w) == 100
    parts = score.Parts(semantic=0.5, skills=0.5, seniority=0.5, freshness=0.5)
    assert score.fit(parts, w) == 50  # llm missing: other weights share its 0.20
    mixed = score.Parts(semantic=0.8, skills=0.6, seniority=1.0, freshness=0.5, llm=0.7)
    assert score.fit(mixed, w) == pytest.approx(
        100 * (0.35 * 0.8 + 0.25 * 0.6 + 0.2 * 0.7 + 0.1 + 0.05), abs=0.1
    )
    assert score.freshness(NOW - timedelta(hours=3), NOW) == 1
    assert score.freshness(NOW - timedelta(days=30), NOW) == 0
    assert score.freshness(NOW - timedelta(days=14), NOW, 14) == 0
    assert 0.5 < score.freshness(NOW - timedelta(days=10), NOW) < 0.75
    assert score.seniority("Security Analyst Intern") == 1 and score.seniority("Analyst", "above") == 0.1
    assert score.priority(80, "linkedin", True, settings) == round(80 * 0.85 * 1.15, 1)


def test_tfidf_prefers_related_text():
    a, b = similarity.tfidf_scores(
        "linux bash siem soc analyst", ["soc analyst linux siem", "sales targets crm"]
    )
    assert a > b


@pytest.fixture
def fake_llm(monkeypatch, settings, profile, conn):
    from agent import llm

    provider = FakeProvider(
        default=json.dumps({"score": 8, "reasons": ["Linux"], "gaps": [], "seniority_fit": "match"})
    )
    monkeypatch.setattr(llm.litellm, "completion", provider)
    monkeypatch.setattr(llm.litellm, "completion_cost", lambda **_: 0.0)

    class Emb:
        def __init__(self, n):
            self.data = [{"embedding": [1.0, 0.5, 0.1]} for _ in range(n)]

    monkeypatch.setattr(llm.litellm, "embedding", lambda **kw: Emb(len(kw["input"])))
    return llm.LLMClient(settings, profile, conn, api_key=lambda: "k", now=lambda: NOW, sleep=lambda s: None)


def test_run_scoring_end_to_end(conn, settings, search, profile, fake_llm):
    _job(conn, "Security Analyst", description="Requirements: Linux, Bash, SIEM")
    _job(conn, "Senior Security Analyst")
    result = score.run_scoring(conn, settings, search, profile, client=fake_llm, now=NOW)
    assert result.filtered == {"passed": 1, "too_senior": 1}
    assert result.scored == 1 and result.rubric_done == 1 and result.semantic_method == "embedding"
    row = conn.execute("SELECT * FROM scores").fetchone()
    assert row["llm_rubric"] == 0.8 and row["priority"] > 0 and json.loads(row["reasons_json"]) == ["Linux"]
    # Cached embeddings: a second run embeds nothing new and rubric is not repeated.
    calls = conn.execute("SELECT count(*) FROM llm_calls").fetchone()[0]
    score.run_scoring(conn, settings, search, profile, client=fake_llm, now=NOW)
    assert conn.execute("SELECT count(*) FROM llm_calls").fetchone()[0] == calls


def test_run_scoring_without_llm_uses_tfidf(conn, settings, search, profile):
    _job(conn, "Security Analyst", description="Linux SIEM")
    result = score.run_scoring(conn, settings, search, profile, client=None, now=NOW)
    assert result.semantic_method == "tfidf" and result.rubric_done == 0
    assert conn.execute("SELECT llm_rubric FROM scores").fetchone()[0] is None


def test_embedding_cache_roundtrip(conn):
    similarity.put_cached(conn, "job:1", "m", [0.25, -1.5])
    assert similarity.get_cached(conn, "job:1", "m") == [0.25, -1.5]
    assert similarity.get_cached(conn, "job:1", "other") is None
    assert db.get_flag(conn, "x") is None


def test_search_change_refilters(conn, search):
    _job(conn, "Account Executive")
    assert filters.run_filters(conn, search, NOW) == {"irrelevant_title": 1}
    assert filters.run_filters(conn, search, NOW) == {}  # nothing new
    wider = search.model_copy(update={"title_keywords": [*search.title_keywords, "account"]})
    assert filters.run_filters(conn, wider, NOW) == {"passed": 1}


def test_rescore_once_without_api_calls(conn, settings, search, profile):
    _job(conn, "Security Analyst", description="Linux SIEM")
    score.run_scoring(conn, settings, search, profile, client=None, now=NOW)
    conn.execute("UPDATE scores SET fit = 1, priority = 1")
    db.set_flag(conn, score.SCORING_FLAG, "old")
    score.run_scoring(conn, settings, search, profile, client=None, now=NOW)
    assert conn.execute("SELECT fit FROM scores").fetchone()[0] > 1
    assert db.get_flag(conn, score.SCORING_FLAG) == score.SCORING_VERSION
    assert conn.execute("SELECT count(*) FROM llm_calls").fetchone()[0] == 0


def test_embedding_waits_out_minute_limit_and_caches_chunks(conn, settings):
    from agent.llm import LLMBusy

    class Client:
        llm = settings.llm
        calls = 0

        def embed(self, stage, texts, run_id=None):
            Client.calls += 1
            if Client.calls == 2:
                raise LLMBusy("minute")
            return [[1.0, 0.0] for _ in texts]

    jobs = [_job(conn, f"Security Analyst {i}") for i in range(60)]
    waits = []
    similarity.embed_missing(conn, Client(), jobs, {}, sleep=waits.append)
    assert waits == [20] and Client.calls == 3
    assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 60


def test_embed_logs_one_request_per_text(fake_llm, conn):
    fake_llm.embed("embed_jobs", ["a", "b", "c"])
    assert conn.execute("SELECT count(*) FROM llm_calls WHERE stage = 'embed_jobs'").fetchone()[0] == 3


def test_years_from_naukri_url_and_yrs(conn, search):
    job_id, _ = upsert(
        conn,
        RawJob("naukri", "n1", "Infosys", "Java Developer", location="Bengaluru",
               url="https://www.naukri.com/job-listings-java-developer-infosys-bengaluru-6-to-10-years-300726033157",
               posted_at=NOW),
        NOW,
    )  # fmt: skip
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert filters.check(job, search, NOW) == "too_senior"
    assert filters.min_years_required("Experience: 5-8 Yrs") == 5


def test_rule_change_also_rechecks_jobs_that_passed(conn, search):
    _job(conn, "Security Analyst")
    assert filters.run_filters(conn, search, NOW) == {"passed": 1}
    stricter = search.model_copy(update={"blocklist_companies": ["Acme"]})
    assert filters.run_filters(conn, stricter, NOW) == {"blocked_company": 1}


def test_scale_embedding_custom_scale():
    assert similarity.scale_embedding(0.50, (0.50, 0.70)) == 0.0
    assert similarity.scale_embedding(0.60, (0.50, 0.70)) == 0.5
    assert similarity.scale_embedding(0.70, (0.50, 0.70)) == 1.0
    assert similarity.scale_embedding(0.80, (0.50, 0.70)) == 1.0
    assert similarity.scale_embedding(0.40, (0.50, 0.70)) == 0.0
    # Custom scale
    assert similarity.scale_embedding(0.75, (0.70, 0.80)) == 0.5
