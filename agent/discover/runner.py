"""Discovery stage: ATS boards for watchlist companies, then JobSpy boards. Writes jobs and source health."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agent import db
from agent.config import Companies, Search, Settings
from agent.discover import ats, jobspy_source
from agent.discover.base import (
    Fetcher,
    RawJob,
    SourceError,
    breaker_open,
    record_failure,
    record_ok,
    upsert,
)
from agent.runs import stage_run

log = logging.getLogger(__name__)


@dataclass
class SourceStats:
    found: int = 0
    new: int = 0
    errors: int = 0
    skipped: bool = False


@dataclass
class DiscoveryResult:
    sources: dict[str, SourceStats] = field(default_factory=dict)
    tripped: list[str] = field(default_factory=list)  # sources whose breaker just opened (alert once)

    @property
    def new(self) -> int:
        return sum(s.new for s in self.sources.values())


def sync_companies(conn: sqlite3.Connection, companies: Companies) -> dict[str, int]:
    """Mirror companies.yaml into the companies table. Returns name -> id."""
    ids = {}
    for company in companies.companies:
        if not company.ats_slug:
            continue
        conn.execute(
            "INSERT INTO companies(name, domain, ats_type, ats_slug, watchlist) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(ats_type, ats_slug) DO UPDATE SET name = excluded.name, domain = excluded.domain,"
            " watchlist = excluded.watchlist",
            (company.name, company.domain, company.ats_type, company.ats_slug, int(company.watchlist)),
        )
        row = conn.execute(
            "SELECT id FROM companies WHERE ats_type = ? AND ats_slug IS ?",
            (company.ats_type, company.ats_slug),
        ).fetchone()
        ids[company.name] = row["id"]
    return ids


def _store(conn: sqlite3.Connection, jobs: list[RawJob], stats: SourceStats, now: datetime, company_id=None):
    with db.transaction(conn):
        for job in jobs:
            if not job.title or not job.external_id:
                continue
            _, is_new = upsert(conn, job, now, company_id)
            stats.found += 1
            stats.new += int(is_new)


def run_discovery(
    conn: sqlite3.Connection,
    settings: Settings,
    search: Search,
    companies: Companies,
    kinds: tuple[str, ...] = ("ats", "jobspy"),
    fetcher: Fetcher | None = None,
    scrape: Callable | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DiscoveryResult:
    result = DiscoveryResult()
    with stage_run(conn, "discover") as run:
        if "ats" in kinds:
            http = fetcher or Fetcher(settings.discovery)
            ids = sync_companies(conn, companies)
            for company in companies.companies:
                if not company.watchlist or company.ats_type not in ("greenhouse", "lever", "ashby"):
                    continue
                stats = result.sources.setdefault(company.ats_type, SourceStats())
                # Breakers are per company board, so one bad slug can't silence a whole ATS.
                key = f"{company.ats_type}:{company.ats_slug}"
                if breaker_open(conn, key, now()):
                    continue
                try:
                    jobs = ats.fetch_company(company, http)
                except SourceError as exc:
                    stats.errors += 1
                    if record_failure(conn, key, str(exc), settings.discovery, now()):
                        result.tripped.append(key)
                    continue
                record_ok(conn, key, now())
                _store(conn, jobs, stats, now(), ids.get(company.name))
        if "jobspy" in kinds:
            for site in search.jobspy_sites:
                stats = result.sources.setdefault(site, SourceStats())
                if breaker_open(conn, site, now()):
                    stats.skipped = True
                    continue
                try:
                    jobs = jobspy_source.fetch_site(
                        site,
                        search,
                        fetch_descriptions=True,
                        **({"scrape": scrape} if scrape else {}),
                    )
                except SourceError as exc:
                    stats.errors += 1
                    if record_failure(conn, site, str(exc), settings.discovery, now()):
                        result.tripped.append(site)
                    continue
                record_ok(conn, site, now())
                _store(conn, jobs, stats, now())
        run.items = result.new
        # A run is unhealthy only when every source failed; single bad boards show in source_health.
        if result.sources and all(s.errors and not s.found for s in result.sources.values()):
            run.error = "all_sources_failed"
    log.info("discover: %s new; %s", result.new, {k: vars(v) for k, v in result.sources.items()})
    return result
