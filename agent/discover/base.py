"""Common job shape, normalisation, dedupe and polite HTTP for every source."""

from __future__ import annotations

import html
import logging
import random
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from agent import __version__, db
from agent.config import DiscoverySettings

log = logging.getLogger(__name__)

USER_AGENT = f"job-agent/{__version__} (personal job search; low volume)"
ATS_SOURCES = ("greenhouse", "lever", "ashby")


@dataclass
class RawJob:
    source: str
    external_id: str
    company: str
    title: str
    url: str | None = None
    apply_url: str | None = None
    location: str | None = None
    remote: bool | None = None
    description: str | None = None
    posted_at: datetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    ats_type: str | None = None


# ---------------------------------------------------------------- normalisation

_COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|ltd|limited|llc|llp|pvt|private|corp|corporation|co|gmbh|plc|technologies|technology)\b"
)
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def norm(text: str | None) -> str:
    return _NON_WORD_RE.sub(" ", (text or "").lower()).strip()


def norm_company(name: str | None) -> str:
    return re.sub(r"\s+", " ", _COMPANY_SUFFIX_RE.sub(" ", norm(name))).strip()


def city_of(location: str | None, remote: bool | None) -> str:
    if remote and not location:
        return "remote"
    first = (location or "").split(",")[0].split("/")[0].split("|")[0]
    city = norm(first)
    return {"bangalore": "bengaluru", "madras": "chennai", "bombay": "mumbai", "gurgaon": "gurugram"}.get(
        city, city
    ) or ("remote" if remote else "")


def dedupe_key(job: RawJob) -> str:
    return f"{norm_company(job.company)}|{norm(job.title)}|{city_of(job.location, job.remote)}"


def html_to_text(markup: str | None) -> str:
    if not markup:
        return ""
    text = BeautifulSoup(html.unescape(markup), "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_dt(value: object) -> datetime | None:
    """ISO strings, epoch milliseconds, dates and datetimes -> aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    if hasattr(value, "year") and hasattr(value, "month"):  # date
        return datetime(value.year, value.month, value.day, tzinfo=UTC)  # type: ignore[attr-defined]
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def detect_ats(url: str | None) -> tuple[str, str | None] | None:
    """('greenhouse', slug) etc. when a URL points at a Tier A ATS posting."""
    if not url:
        return None
    parsed = urlparse(url)
    host, parts = parsed.netloc.lower(), [p for p in parsed.path.split("/") if p]
    if host.endswith("greenhouse.io"):
        if parts and parts[0] == "embed":
            slug = parse_qs(parsed.query).get("for", [None])[0]
            return "greenhouse", slug
        return "greenhouse", parts[0] if parts else None
    if host == "jobs.lever.co":
        return "lever", parts[0] if parts else None
    if host == "jobs.ashbyhq.com":
        return "ashby", parts[0] if parts else None
    return None


def resolve_shortlink(url: str | None, timeout: float = 5.0) -> str | None:
    """Follow single HTTP redirect for grnh.se links (GET, redirects off, 5s timeout, read Location)."""
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.netloc.lower() in ("grnh.se", "www.grnh.se") or parsed.netloc.lower().endswith(".grnh.se"):
        try:
            resp = httpx.get(url, follow_redirects=False, timeout=timeout)
            loc = resp.headers.get("Location") or resp.headers.get("location")
            if loc:
                return loc
        except Exception as exc:
            log.warning("failed to resolve shortlink %s: %s", url, exc)
    return url


def to_greenhouse_embed(url: str | None, slug: str | None = None, token: str | None = None) -> str | None:
    """Rewrite greenhouse job URL into standard embed application URL."""
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("greenhouse.io"):
        return url
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0] == "embed":
        return url
    if not slug and parts:
        slug = parts[0]
    if not token and len(parts) >= 3 and parts[1] == "jobs":
        token = parts[2]
    elif not token and len(parts) >= 2 and parts[0] == "jobs":
        token = parts[1]
    query = parse_qs(parsed.query)
    if not token and "token" in query:
        token = query["token"][0]
    if slug and token:
        return f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={token}"
    return url


def resolve_existing_shortlinks(conn: sqlite3.Connection) -> int:
    """Find existing jobs with grnh.se links, resolve redirect, set embed apply_url & ats_type."""
    rows = conn.execute(
        "SELECT id, url, apply_url FROM jobs WHERE apply_url LIKE '%grnh.se%' OR url LIKE '%grnh.se%'"
    ).fetchall()
    updated = 0
    for row in rows:
        target = row["apply_url"] or row["url"]
        loc = resolve_shortlink(target)
        if loc and loc != target:
            ats = detect_ats(loc)
            apply_url = loc
            if ats and ats[0] == "greenhouse":
                apply_url = to_greenhouse_embed(loc, slug=ats[1]) or loc
            ats_type = ats[0] if ats else None
            conn.execute(
                "UPDATE jobs SET apply_url = ?, ats_type = COALESCE(?, ats_type) WHERE id = ?",
                (apply_url, ats_type, row["id"]),
            )
            updated += 1
    if updated:
        conn.commit()
    return updated


# ---------------------------------------------------------------- storage


def upsert(
    conn: sqlite3.Connection, job: RawJob, now: datetime, company_id: int | None = None
) -> tuple[int, bool]:
    """Insert a job, or refresh the one it duplicates. Returns (job_id, is_new).

    A duplicate found later on an employer ATS upgrades the stored apply link and description,
    because ATS-direct postings are the most complete and the ones the agent can apply to.
    """
    stamp = db.to_iso(now)
    row = conn.execute(
        "SELECT id FROM jobs WHERE source = ? AND external_id = ?", (job.source, job.external_id)
    ).fetchone()
    if row:
        conn.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ?", (stamp, row["id"]))
        return row["id"], False
    key = dedupe_key(job)
    row = conn.execute("SELECT id, ats_type, description FROM jobs WHERE dedupe_key = ?", (key,)).fetchone()
    if row:
        if job.ats_type in ATS_SOURCES and row["ats_type"] not in ATS_SOURCES:
            conn.execute(
                "UPDATE jobs SET apply_url = ?, ats_type = ?, description = coalesce(?, description),"
                " last_seen_at = ? WHERE id = ?",
                (job.apply_url or job.url, job.ats_type, job.description or None, stamp, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET last_seen_at = ?, description = coalesce(description, ?) WHERE id = ?",
                (stamp, job.description or None, row["id"]),
            )
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO jobs(source, external_id, url, apply_url, company, company_id, title, location, remote,"
        " salary_min, salary_max, currency, posted_at, first_seen_at, last_seen_at, description, dedupe_key,"
        " ats_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job.source,
            job.external_id,
            job.url,
            job.apply_url or job.url,
            job.company,
            company_id,
            job.title,
            job.location,
            None if job.remote is None else int(job.remote),
            job.salary_min,
            job.salary_max,
            job.currency,
            db.to_iso(job.posted_at) if job.posted_at else None,
            stamp,
            stamp,
            job.description or None,
            key,
            job.ats_type,
        ),
    )
    return cur.lastrowid, True


# ---------------------------------------------------------------- polite HTTP


class SourceError(Exception):
    """A source could not be read this run (network, block, bad response)."""


class Fetcher:
    """One request at a time per host, random 2-5 s gaps, backoff on 429/5xx, honest user agent."""

    def __init__(
        self,
        settings: DiscoverySettings,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.client = client or httpx.Client(
            timeout=settings.request_timeout_s, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def _wait(self, host: str) -> None:
        gap = random.uniform(
            self.settings.min_delay_s, max(self.settings.min_delay_s, self.settings.max_delay_s)
        )
        last = self._last.get(host)
        if last is not None:
            remaining = gap - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()

    def get_json(self, url: str, params: dict | None = None) -> object:
        host = urlparse(url).netloc
        for attempt in range(3):
            self._wait(host)
            try:
                response = self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise SourceError(f"network: {type(exc).__name__}") from exc
                self._sleep(5 * (attempt + 1))
                continue
            if response.status_code == 404:
                raise SourceError("not found (check the ATS slug)")
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == 2:
                    raise SourceError(f"http {response.status_code}")
                self._sleep(10 * (attempt + 1))
                continue
            if response.status_code != 200:
                raise SourceError(f"http {response.status_code}")
            try:
                return response.json()
            except ValueError as exc:
                raise SourceError("invalid json") from exc
        raise SourceError("unreachable")


# ---------------------------------------------------------------- circuit breaker


def breaker_open(conn: sqlite3.Connection, source: str, now: datetime) -> bool:
    row = conn.execute("SELECT disabled_until FROM source_health WHERE source = ?", (source,)).fetchone()
    return bool(row and row["disabled_until"] and db.parse_iso(row["disabled_until"]) > now)


def record_ok(conn: sqlite3.Connection, source: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO source_health(source, consecutive_failures, last_ok_at) VALUES (?, 0, ?)"
        " ON CONFLICT(source) DO UPDATE SET consecutive_failures = 0, disabled_until = NULL,"
        " last_ok_at = excluded.last_ok_at",
        (source, db.to_iso(now)),
    )


def record_failure(
    conn: sqlite3.Connection, source: str, error: str, settings: DiscoverySettings, now: datetime
) -> bool:
    """Returns True when this failure trips the breaker (so the caller alerts exactly once)."""
    conn.execute(
        "INSERT INTO source_health(source, consecutive_failures, last_error) VALUES (?, 1, ?)"
        " ON CONFLICT(source) DO UPDATE SET consecutive_failures = consecutive_failures + 1,"
        " last_error = excluded.last_error",
        (source, error[:200]),
    )
    failures = conn.execute(
        "SELECT consecutive_failures FROM source_health WHERE source = ?", (source,)
    ).fetchone()[0]
    if failures == settings.breaker_failures:
        until = now + timedelta(hours=settings.breaker_hours)
        conn.execute(
            "UPDATE source_health SET disabled_until = ? WHERE source = ?", (db.to_iso(until), source)
        )
        return True
    if failures > settings.breaker_failures:
        # Half-open retry failed again: back off for another period, but don't alert again.
        until = now + timedelta(hours=settings.breaker_hours)
        conn.execute(
            "UPDATE source_health SET disabled_until = ? WHERE source = ?", (db.to_iso(until), source)
        )
    return False
