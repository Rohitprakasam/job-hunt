"""Hard filters. A job that fails one is never scored; the reason is stored so you can see why."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from agent import db
from agent.config import Search
from agent.discover.base import city_of, norm, norm_company

SEARCH_HASH_FLAG = "search_config_hash"
# Bump when the filter rules change, so stored jobs are checked again.
FILTER_VERSION = "4"
_URL_YEARS_RE = re.compile(r"-(\d{1,2})-to-\d{1,2}-years", re.IGNORECASE)

# Words too generic to mark a title as relevant on their own.
_GENERIC = {
    "and",
    "or",
    "of",
    "the",
    "a",
    "an",
    "in",
    "for",
    "to",
    "with",
    "i",
    "ii",
    "iii",
    "intern",
    "junior",
}
_YEARS_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?(?:\+\s*)?(?:years?|yrs?)\b",
    re.IGNORECASE,
)


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def tokens(text: str | None) -> set[str]:
    return {_stem(w) for w in norm(text).split()}


def title_words(search: Search) -> set[str]:
    words = search.title_keywords or [w for role in search.roles for w in norm(role).split()]
    return {_stem(norm(w)) for w in words if norm(w) and norm(w) not in _GENERIC}


def min_years_required(text: str) -> int | None:
    """Smallest 'N years' / 'N+ years' / 'N-M years' figure near experience wording, if any."""
    found = []
    for match in _YEARS_RE.finditer(text or ""):
        window = text[max(0, match.start() - 60) : match.end() + 60].lower()
        if "experience" in window or "exp." in window or "yr" in match.group(0).lower():
            found.append(int(match.group(1)))
    return min(found) if found else None


# Target city -> state names and codes job boards use instead of the city.
STATES = {
    "chennai": {"tn", "tamil", "nadu"},
    "coimbatore": {"tn", "tamil", "nadu"},
    "bengaluru": {"ka", "karnataka"},
    "hyderabad": {"ts", "tg", "telangana"},
    "mumbai": {"mh", "maharashtra"},
    "pune": {"mh", "maharashtra"},
    "delhi": {"dl", "delhi", "ncr"},
    "gurugram": {"hr", "haryana", "ncr"},
    "noida": {"up", "ncr"},
    "kochi": {"kl", "kerala"},
}
_ANYWHERE = {"remote", "anywhere", "worldwide", "global", "apac", "asia", "work from home", "wfh", "hybrid"}


def _remote_ok_for(location: str, search: Search, wanted: set[str]) -> bool:
    """'Remote' alone or 'Remote - India' is fine; 'Remote - USA' is a remote job for someone else."""
    rest = norm(location)
    for word in _ANYWHERE:
        rest = rest.replace(word, " ")
    rest = rest.strip()
    if not rest:
        return True
    return norm(search.country_indeed) in rest or any(w and w in rest for w in wanted)


def _location_ok(job: Any, search: Search) -> bool:
    remote = bool(job["remote"])
    location = (job["location"] or "").lower()
    wanted = {city_of(loc, False) for loc in search.locations if loc.lower() != "remote"}
    if search.remote_ok and (remote or "remote" in location):
        return _remote_ok_for(location, search, wanted)
    if not location:
        return True  # unknown: let scoring decide
    if city_of(job["location"], remote) in wanted:
        return True
    words = set(norm(location).split())
    if words <= {norm(search.country_indeed)}:
        return True  # just "India": the board matched our city search, keep it for scoring
    # Boards often give the state, not the city ("TN, India" for a Chennai search).
    if any(words & STATES.get(w, set()) for w in wanted):
        return True
    return any(w and w in norm(location) for w in wanted)


def check(job: Any, search: Search, now: datetime) -> str | None:
    """Returns a short reason code, or None when the job passes every hard filter."""
    title = norm(job["title"])
    text = f"{job['title']}\n{job['description'] or ''}".lower()

    posted = job["posted_at"] or job["first_seen_at"]
    if posted and db.parse_iso(posted) < now - timedelta(days=search.max_posting_age_days):
        return "too_old"
    if norm_company(job["company"]) in {norm_company(c) for c in search.blocklist_companies}:
        return "blocked_company"
    if any(k.lower() in text for k in search.blocklist_keywords):
        return "blocked_keyword"
    words = title_words(search)
    if words and not (tokens(title) & words):
        return "irrelevant_title"
    if set(title.split()) & {norm(w) for w in search.exclude_title_words}:
        return "too_senior"
    years = min_years_required(job["description"] or "")
    in_url = _URL_YEARS_RE.search(
        f"{job['url'] or ''} {job['apply_url'] or ''}"
    )  # naukri: ...-6-to-11-years-...
    if in_url:
        years = min(years, int(in_url.group(1))) if years is not None else int(in_url.group(1))
    if years is not None and years > search.max_years_required:
        return "too_senior"
    if not _location_ok(job, search):
        return "location"
    floor = search.salary_floor
    salary_max = job["salary_max"]
    if floor and salary_max and (job["currency"] or search.currency) == search.currency:
        # Only compare figures that look annual; monthly or hourly ones would be rejected wrongly.
        if salary_max >= floor / 4 and salary_max < floor:
            return "below_salary"
    return None


def run_filters(conn, search: Search, now: datetime) -> dict[str, int]:
    """Filter every job not filtered yet. Returns counts per reason ('passed' for the rest).
    When search.yaml changes, jobs that were filtered out are checked again under the new rules."""
    counts: dict[str, int] = {}
    digest = hashlib.sha256((FILTER_VERSION + search.model_dump_json()).encode()).hexdigest()[:16]
    if db.get_flag(conn, SEARCH_HASH_FLAG) != digest:
        conn.execute(
            "UPDATE jobs SET filtered_at = NULL, filter_reason = NULL"
            " WHERE id NOT IN (SELECT job_id FROM applications)"
        )
        db.set_flag(conn, SEARCH_HASH_FLAG, digest)
    rows = conn.execute("SELECT * FROM jobs WHERE filtered_at IS NULL").fetchall()
    with db.transaction(conn):
        for job in rows:
            reason = check(job, search, now)
            conn.execute(
                "UPDATE jobs SET filter_reason = ?, filtered_at = ? WHERE id = ?",
                (reason, db.to_iso(now), job["id"]),
            )
            counts[reason or "passed"] = counts.get(reason or "passed", 0) + 1
    return counts
