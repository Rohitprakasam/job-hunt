"""Job boards through JobSpy (LinkedIn, Indeed, Naukri, ...). One query per site so a block on one
site trips only that site's circuit breaker."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from agent.config import Search
from agent.discover.base import (
    RawJob,
    SourceError,
    detect_ats,
    parse_dt,
    resolve_shortlink,
    to_greenhouse_embed,
)

log = logging.getLogger(__name__)


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if str(value) in {"nan", "NaT", "None", ""}:
        return None
    return value


def rows_to_jobs(rows: list[dict[str, Any]], site: str) -> list[RawJob]:
    out = []
    for raw in rows:
        row = {k: _clean(v) for k, v in raw.items()}
        if not row.get("title") or not row.get("company"):
            continue
        direct = row.get("job_url_direct")
        target_url = direct or row.get("job_url")
        resolved = resolve_shortlink(target_url) if target_url else None
        candidate = resolved or direct
        ats = detect_ats(candidate)
        apply_url = candidate or row.get("job_url")
        if ats and ats[0] == "greenhouse":
            apply_url = to_greenhouse_embed(apply_url, slug=ats[1]) or apply_url
        out.append(
            RawJob(
                source=site,
                external_id=str(row.get("id") or row.get("job_url")),
                company=str(row["company"]).strip(),
                title=str(row["title"]).strip(),
                url=row.get("job_url"),
                apply_url=apply_url,
                location=row.get("location"),
                remote=bool(row["is_remote"]) if row.get("is_remote") is not None else None,
                description=row.get("description"),
                posted_at=parse_dt(row.get("date_posted")),
                salary_min=row.get("min_amount"),
                salary_max=row.get("max_amount"),
                currency=row.get("currency"),
                ats_type=ats[0] if ats else None,
            )
        )
    return out


def _scrape(**kwargs: Any) -> list[dict[str, Any]]:
    from jobspy import scrape_jobs  # heavy import (pandas); only when discovery runs

    frame = scrape_jobs(**kwargs)
    return frame.to_dict("records") if frame is not None else []


def fetch_site(
    site: str,
    search: Search,
    fetch_descriptions: bool,
    scrape: Callable[..., list[dict[str, Any]]] | None = None,
) -> list[RawJob]:
    scrape = scrape or _scrape
    jobs: list[RawJob] = []
    errors = 0
    queries = 0
    places = [loc for loc in search.locations if loc.lower() != "remote"] or [search.country_indeed]
    for role in search.roles:
        targets = [(place, False) for place in places]
        if search.remote_ok:
            targets.append((search.country_indeed, True))
        for place, remote in targets:
            queries += 1
            try:
                rows = scrape(
                    site_name=[site],
                    search_term=role,
                    location=place,
                    is_remote=remote,
                    results_wanted=search.jobspy_results_per_query,
                    hours_old=search.jobspy_hours_old,
                    country_indeed=search.country_indeed,
                    description_format="markdown",
                    linkedin_fetch_description=fetch_descriptions,
                    verbose=0,
                )
            except Exception as exc:  # JobSpy raises many types; any of them means "this query failed"
                errors += 1
                log.warning("jobspy %s query failed: %s", site, type(exc).__name__)
                continue
            jobs.extend(rows_to_jobs(rows, site))
    if queries and errors == queries:
        raise SourceError(f"{site}: every query failed")
    return jobs
