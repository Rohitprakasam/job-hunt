from datetime import UTC, datetime

import httpx
import pytest

from agent import config
from agent.discover import ats, jobspy_source
from agent.discover.base import (
    Fetcher,
    RawJob,
    SourceError,
    breaker_open,
    dedupe_key,
    detect_ats,
    html_to_text,
    parse_dt,
    record_failure,
    record_ok,
    resolve_existing_shortlinks,
    resolve_shortlink,
    to_greenhouse_embed,
    upsert,
)
from agent.discover.runner import run_discovery

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)

GREENHOUSE_JSON = {
    "jobs": [
        {
            "id": 101,
            "title": "Security Analyst I",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101",
            "location": {"name": "Chennai, India"},
            "updated_at": "2026-10-28T10:00:00-04:00",
            "first_published": "2026-10-28T09:00:00-04:00",
            "content": "&lt;p&gt;We need &lt;b&gt;Linux&lt;/b&gt; and SIEM skills.&lt;/p&gt;",
            "company_name": "Acme Corp",
        }
    ]
}
LEVER_JSON = [
    {
        "id": "abc-123",
        "text": "Linux Administrator",
        "hostedUrl": "https://jobs.lever.co/beta/abc-123",
        "applyUrl": "https://jobs.lever.co/beta/abc-123/apply",
        "categories": {"location": "Bengaluru"},
        "createdAt": 1761638400000,
        "descriptionPlain": "Run our fleet.",
        "lists": [{"text": "Requirements", "content": "<li>Bash</li><li>Ansible</li>"}],
        "workplaceType": "onsite",
    }
]
ASHBY_JSON = {
    "jobs": [
        {
            "id": "z1",
            "title": "SOC Analyst",
            "location": "Remote",
            "isRemote": True,
            "isListed": True,
            "jobUrl": "https://jobs.ashbyhq.com/gamma/z1",
            "applyUrl": "https://jobs.ashbyhq.com/gamma/z1/application",
            "descriptionPlain": "Monitor alerts.",
            "publishedAt": "2026-10-28T00:00:00.000Z",
        },
        {"id": "z2", "title": "Hidden", "isListed": False},
    ]
}


def test_parsers():
    gh = ats.parse_greenhouse(GREENHOUSE_JSON, "Acme")[0]
    assert (gh.company, gh.title, gh.ats_type) == ("Acme Corp", "Security Analyst I", "greenhouse")
    assert gh.description == "We need \nLinux\n and SIEM skills."
    assert gh.posted_at == datetime(2026, 10, 28, 13, 0, tzinfo=UTC)
    lv = ats.parse_lever(LEVER_JSON, "Beta")[0]
    assert lv.apply_url.endswith("/apply") and "Ansible" in lv.description and lv.remote is None
    ash = ats.parse_ashby(ASHBY_JSON, "Gamma")
    assert [j.external_id for j in ash] == ["z1"] and ash[0].remote is True
    with pytest.raises(SourceError):
        ats.parse_greenhouse({"oops": 1}, "x")


def test_helpers():
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") == ("greenhouse", "acme")
    assert detect_ats("https://jobs.lever.co/beta/x/apply") == ("lever", "beta")
    assert detect_ats("https://jobs.ashbyhq.com/gamma/z1") == ("ashby", "gamma")
    assert detect_ats("https://job-boards.greenhouse.io/embed/job_app?for=deepintent&token=6241728004") == (
        "greenhouse",
        "deepintent",
    )
    assert detect_ats("https://example.com/jobs/1") is None
    assert parse_dt(1761638400000) == parse_dt(1761638400) == datetime(2025, 10, 28, 8, 0, tzinfo=UTC)
    assert parse_dt("not a date") is None
    assert html_to_text("&lt;p&gt;a&lt;/p&gt;") == "a"
    a = RawJob("linkedin", "1", "Acme Pvt Ltd", "Security Analyst", location="Bangalore, KA")
    b = RawJob("greenhouse", "9", "ACME", "Security  Analyst", location="Bengaluru")
    assert dedupe_key(a) == dedupe_key(b) == "acme|security analyst|bengaluru"


def test_upsert_dedupes_and_upgrades_to_ats(conn):
    board = RawJob(
        "linkedin", "li-1", "Acme", "Security Analyst", url="https://linkedin.com/x", location="Chennai"
    )
    job_id, new = upsert(conn, board, NOW)
    assert new
    assert upsert(conn, board, NOW) == (job_id, False)
    direct = RawJob(
        "greenhouse", "101", "Acme Inc", "Security Analyst", apply_url="https://job-boards.greenhouse.io/acme/jobs/101",
        location="Chennai, India", description="full text", ats_type="greenhouse",
    )  # fmt: skip
    assert upsert(conn, direct, NOW) == (job_id, False)
    row = conn.execute("SELECT * FROM jobs").fetchall()
    assert len(row) == 1 and row[0]["ats_type"] == "greenhouse" and row[0]["description"] == "full text"


def test_breaker(conn, settings):
    d = settings.discovery
    assert not record_failure(conn, "linkedin", "http 429", d, NOW)
    assert not record_failure(conn, "linkedin", "http 429", d, NOW)
    assert record_failure(conn, "linkedin", "http 429", d, NOW)  # third trips it, alert once
    assert breaker_open(conn, "linkedin", NOW)
    assert not record_failure(conn, "linkedin", "again", d, NOW)  # no second alert
    record_ok(conn, "linkedin", NOW)
    assert not breaker_open(conn, "linkedin", NOW)


def _fetcher(settings, routes):
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, (status, body) in routes.items():
            if str(request.url).startswith(prefix):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return Fetcher(
        settings.discovery, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None
    )


def test_run_discovery_ats_and_jobspy(conn, settings):
    companies = config.Companies(
        companies=[
            config.Company(name="Acme", ats_type="greenhouse", ats_slug="acme"),
            config.Company(name="Beta", ats_type="lever", ats_slug="beta"),
            config.Company(name="Gone", ats_type="ashby", ats_slug="gone"),
        ]
    )
    search = config.load_search(
        __import__("tests.conftest", fromlist=["EXAMPLES"]).EXAMPLES / "search.example.yaml"
    )
    fetcher = _fetcher(
        settings,
        {
            ats.GREENHOUSE.format(slug="acme"): (200, GREENHOUSE_JSON),
            ats.LEVER.format(slug="beta"): (200, LEVER_JSON),
        },
    )
    calls = []

    def scrape(**kwargs):
        calls.append(kwargs)
        if kwargs["site_name"] == ["indeed"]:
            raise RuntimeError("blocked")
        return [
            {
                "id": f"{kwargs['site_name'][0]}-{kwargs['search_term']}",
                "site": kwargs["site_name"][0],
                "title": "Security Analyst I",
                "company": "Acme Corp",  # duplicate of the Greenhouse job
                "location": "Chennai, India",
                "job_url": "https://linkedin.com/jobs/1",
                "job_url_direct": float("nan"),
                "is_remote": False,
                "description": "SIEM",
                "date_posted": None,
            }
        ]

    result = run_discovery(conn, settings, search, companies, fetcher=fetcher, scrape=scrape, now=lambda: NOW)
    assert result.sources["greenhouse"].new == 1 and result.sources["lever"].new == 1
    assert result.sources["ashby"].errors == 1
    assert result.sources["indeed"].errors == 1 and result.sources["linkedin"].new == 0
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    assert {c["linkedin_fetch_description"] for c in calls} == {True}


def test_jobspy_rows_detect_ats():
    rows = [
        {
            "id": "1",
            "title": "T",
            "company": "C",
            "job_url": "u",
            "job_url_direct": "https://jobs.lever.co/c/1",
        }
    ]
    job = jobspy_source.rows_to_jobs(rows, "linkedin")[0]
    assert job.ats_type == "lever" and job.apply_url == "https://jobs.lever.co/c/1"


def test_greenhouse_embed_rewriting():
    url = "https://job-boards.greenhouse.io/deepintent/jobs/6241728004?gh_src=y4wa63624us"
    embed = to_greenhouse_embed(url)
    assert embed == "https://job-boards.greenhouse.io/embed/job_app?for=deepintent&token=6241728004"
    # Calling on already embedded url leaves it intact
    assert to_greenhouse_embed(embed) == embed


def test_resolve_shortlink(monkeypatch):
    def fake_get(url, follow_redirects=False, timeout=5.0):
        assert url == "https://grnh.se/y4wa63624us"
        return httpx.Response(
            302,
            headers={
                "Location": "https://job-boards.greenhouse.io/deepintent/jobs/6241728004?gh_src=y4wa63624us"
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    resolved = resolve_shortlink("https://grnh.se/y4wa63624us")
    assert resolved == "https://job-boards.greenhouse.io/deepintent/jobs/6241728004?gh_src=y4wa63624us"


def test_resolve_existing_shortlinks_db(monkeypatch, conn):
    conn.execute(
        "INSERT INTO jobs (id, source, external_id, title, company, url, apply_url, ats_type, first_seen_at, dedupe_key)"
        " VALUES (6351, 'jobspy', 'ext-1', 'AI Engineer', 'DeepIntent', 'https://grnh.se/y4wa63624us', 'https://grnh.se/y4wa63624us', NULL, '2026-11-08T00:00:00Z', 'deepintent|ai engineer')"
    )

    def fake_get(url, follow_redirects=False, timeout=5.0):
        return httpx.Response(
            302,
            headers={
                "Location": "https://job-boards.greenhouse.io/deepintent/jobs/6241728004?gh_src=y4wa63624us"
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    count = resolve_existing_shortlinks(conn)
    assert count == 1
    row = conn.execute("SELECT apply_url, ats_type FROM jobs WHERE id = 6351").fetchone()
    assert row["ats_type"] == "greenhouse"
    assert (
        row["apply_url"] == "https://job-boards.greenhouse.io/embed/job_app?for=deepintent&token=6241728004"
    )
