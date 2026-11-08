"""Public job-board JSON for Greenhouse, Lever and Ashby. No auth; one request per company."""

from __future__ import annotations

from typing import Any

from agent.config import Company
from agent.discover.base import Fetcher, RawJob, SourceError, html_to_text, parse_dt

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER = "https://api.lever.co/v0/postings/{slug}"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def _expect(data: Any, kind: type, what: str) -> Any:
    if not isinstance(data, kind):
        raise SourceError(f"unexpected {what} response shape")
    return data


def parse_greenhouse(data: Any, company: str, slug: str | None = None) -> list[RawJob]:
    jobs = _expect(_expect(data, dict, "greenhouse").get("jobs"), list, "greenhouse")
    out = []
    for item in jobs:
        location = (item.get("location") or {}).get("name")
        external_id = str(item["id"])
        apply_url = (
            f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={external_id}"
            if slug
            else item.get("absolute_url")
        )
        out.append(
            RawJob(
                source="greenhouse",
                external_id=external_id,
                company=item.get("company_name") or company,
                title=item.get("title", "").strip(),
                url=item.get("absolute_url"),
                apply_url=apply_url,
                location=location,
                remote="remote" in (location or "").lower(),
                description=html_to_text(item.get("content")),
                posted_at=parse_dt(item.get("first_published") or item.get("updated_at")),
                ats_type="greenhouse",
            )
        )
    return out


def parse_lever(data: Any, company: str) -> list[RawJob]:
    out = []
    for item in _expect(data, list, "lever"):
        categories = item.get("categories") or {}
        sections = "\n\n".join(
            f"{block.get('text', '')}\n{html_to_text(block.get('content'))}"
            for block in item.get("lists") or []
        )
        description = "\n\n".join(
            part for part in (item.get("descriptionPlain"), sections, item.get("additionalPlain")) if part
        )
        salary = item.get("salaryRange") or {}
        out.append(
            RawJob(
                source="lever",
                external_id=str(item["id"]),
                company=company,
                title=item.get("text", "").strip(),
                url=item.get("hostedUrl"),
                apply_url=item.get("applyUrl") or item.get("hostedUrl"),
                location=categories.get("location"),
                remote=(item.get("workplaceType") == "remote") or None,
                description=description.strip(),
                posted_at=parse_dt(item.get("createdAt")),
                salary_min=salary.get("min"),
                salary_max=salary.get("max"),
                currency=salary.get("currency"),
                ats_type="lever",
            )
        )
    return out


def parse_ashby(data: Any, company: str) -> list[RawJob]:
    jobs = _expect(_expect(data, dict, "ashby").get("jobs"), list, "ashby")
    out = []
    for item in jobs:
        if item.get("isListed") is False:
            continue
        out.append(
            RawJob(
                source="ashby",
                external_id=str(item["id"]),
                company=company,
                title=item.get("title", "").strip(),
                url=item.get("jobUrl"),
                apply_url=item.get("applyUrl") or item.get("jobUrl"),
                location=item.get("location"),
                remote=bool(item.get("isRemote")) or item.get("workplaceType") == "Remote" or None,
                description=item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml")),
                posted_at=parse_dt(item.get("publishedAt")),
                ats_type="ashby",
            )
        )
    return out


def fetch_company(company: Company, http: Fetcher) -> list[RawJob]:
    if not company.ats_slug:
        raise SourceError(f"{company.name}: no ats_slug")
    slug = company.ats_slug
    if company.ats_type == "greenhouse":
        return parse_greenhouse(
            http.get_json(GREENHOUSE.format(slug=slug), {"content": "true"}), company.name, slug
        )
    if company.ats_type == "lever":
        return parse_lever(http.get_json(LEVER.format(slug=slug), {"mode": "json"}), company.name)
    if company.ats_type == "ashby":
        return parse_ashby(
            http.get_json(ASHBY.format(slug=slug), {"includeCompensation": "true"}), company.name
        )
    raise SourceError(f"{company.name}: ats_type {company.ats_type} has no public board reader")
