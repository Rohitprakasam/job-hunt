"""Automatic company ATS discovery: probes Greenhouse, Lever and Ashby for employers seen on job boards."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from agent import db, paths

log = logging.getLogger(__name__)

INDIA_LOCATIONS = frozenset(
    [
        "india",
        "bengaluru",
        "bangalore",
        "hyderabad",
        "chennai",
        "pune",
        "mumbai",
        "delhi",
        "gurugram",
        "gurgaon",
        "noida",
        "kolkata",
        "ahmedabad",
        "kochi",
        "cochin",
        "thiruvananthapuram",
        "trivandrum",
        "coimbatore",
        "indore",
        "jaipur",
        "chandigarh",
    ]
)


def jaccard_similarity(a: str, b: str) -> float:
    """Token Jaccard similarity between two strings."""
    set_a = set(re.findall(r"\w+", a.lower()))
    set_b = set(re.findall(r"\w+", b.lower()))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def is_india_location(text: str | None) -> bool:
    if not text:
        return False
    lower = text.lower()
    if any(loc in lower for loc in INDIA_LOCATIONS):
        return True
    return bool(re.search(r"\b(in|ind)\b", lower))


def generate_slugs(company_name: str) -> list[str]:
    """Generate ATS slug candidates from company name."""
    cleaned = re.sub(
        r"(?i)\b(private limited|pvt\.?|ltd\.?|technologies|technology|tech|india|services|solutions|corp\.?|corporation|inc\.?)\b",
        "",
        company_name,
    )
    cleaned = re.sub(r"[^\w\s-]", "", cleaned).strip().lower()
    if not cleaned:
        return []
    words = cleaned.split()
    if not words:
        return []
    slugs = []
    # 1. Without spaces
    no_spaces = "".join(words)
    if no_spaces and len(no_spaces) >= 2:
        slugs.append(no_spaces)
    # 2. Hyphenated
    hyphenated = "-".join(words)
    if hyphenated and hyphenated not in slugs and len(hyphenated) >= 2:
        slugs.append(hyphenated)
    # 3. First word
    first_word = words[0]
    if first_word and first_word not in slugs and len(first_word) >= 2:
        slugs.append(first_word)
    return slugs


def probe_greenhouse(
    client: httpx.Client,
    slug: str,
    company_name: str,
) -> tuple[int, bool, str | None]:
    """Probe Greenhouse board for jobs in India and matching company name."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        resp = client.get(url, timeout=10)
    except Exception:
        return 0, False, None

    status = resp.status_code
    if status != 200:
        return status, False, None

    data = resp.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    if not jobs:
        return 200, False, None

    has_india = False
    for item in jobs:
        loc = (item.get("location") or {}).get("name")
        if is_india_location(loc):
            has_india = True
            break
    if not has_india:
        return 200, False, None

    # Verify board name
    board_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
    try:
        b_resp = client.get(board_url, timeout=10)
        if b_resp.status_code == 200:
            b_name = b_resp.json().get("name", "")
            if b_name and jaccard_similarity(b_name, company_name) >= 0.5:
                return 200, True, b_name
    except Exception:
        pass
    return 200, False, None


def probe_lever(
    client: httpx.Client,
    slug: str,
    sample_titles: list[str],
) -> tuple[int, bool]:
    """Probe Lever postings for jobs in India and matching job titles."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = client.get(url, timeout=10)
    except Exception:
        return 0, False

    status = resp.status_code
    if status != 200:
        return status, False

    data = resp.json()
    if not isinstance(data, list) or not data:
        return 200, False

    has_india = False
    board_titles = []
    for item in data:
        loc = (item.get("categories") or {}).get("location")
        country = item.get("country")
        if country == "IN" or is_india_location(loc):
            has_india = True
        title = item.get("text", "")
        if title:
            board_titles.append(title)

    if not has_india:
        return 200, False

    # Check title match against known job board titles
    for bt in board_titles:
        for st in sample_titles:
            if jaccard_similarity(bt, st) >= 0.6:
                return 200, True
    return 200, False


def probe_ashby(
    client: httpx.Client,
    slug: str,
    sample_titles: list[str],
) -> tuple[int, bool]:
    """Probe Ashby job-board for jobs in India and matching job titles."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        resp = client.get(url, timeout=10)
    except Exception:
        return 0, False

    status = resp.status_code
    if status != 200:
        return status, False

    data = resp.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    if not jobs:
        return 200, False

    has_india = False
    board_titles = []
    for item in jobs:
        loc = item.get("location")
        if is_india_location(loc):
            has_india = True
        title = item.get("title", "")
        if title:
            board_titles.append(title)

    if not has_india:
        return 200, False

    for bt in board_titles:
        for st in sample_titles:
            if jaccard_similarity(bt, st) >= 0.6:
                return 200, True
    return 200, False


def discover_companies(
    conn: sqlite3.Connection,
    companies_yaml_path: Path | None = None,
    limit: int = 200,
    http_client: httpx.Client | None = None,
    delay_s: float = 1.0,
) -> list[dict[str, Any]]:
    """Probes top distinct company names on ATS endpoints (max 1 req/sec)."""
    now_utc = datetime.now(UTC)
    now_iso = db.to_iso(now_utc)
    target_yaml = companies_yaml_path or (paths.config_dir() / "companies.yaml")

    # 1. Existing companies
    existing_slugs = set()
    if target_yaml.exists():
        content = target_yaml.read_text(encoding="utf-8")
        existing_slugs = set(re.findall(r"ats_slug:\s*([a-zA-Z0-9_-]+)", content))

    # 2. Recently probed companies in company_probes (last 30 days)
    probed_since = db.to_iso(now_utc - timedelta(days=30))
    recently_probed = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT name FROM company_probes WHERE checked_at >= ?", (probed_since,)
        ).fetchall()
    }

    # 3. Candidate names from jobs (last 30 days) + company_seeds.txt
    seeds_file = paths.config_dir() / "company_seeds.txt"
    seed_names = []
    if seeds_file.exists():
        seed_names = [
            line.strip() for line in seeds_file.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    candidates = [
        row[0]
        for row in conn.execute(
            "SELECT company, count(*) as c FROM jobs"
            " WHERE source IN ('indeed', 'linkedin', 'naukri') AND first_seen_at >= ?"
            " GROUP BY company ORDER BY c DESC LIMIT ?",
            (probed_since, limit * 2),
        ).fetchall()
    ]
    all_names = []
    seen = set()
    for name in seed_names + candidates:
        norm = name.strip()
        if norm and norm.lower() not in seen and norm not in recently_probed:
            seen.add(norm.lower())
            all_names.append(norm)

    client = http_client or httpx.Client(headers={"User-Agent": "job-agent/0.1.0"}, follow_redirects=True)
    discovered: list[dict[str, Any]] = []

    try:
        count = 0
        for comp_name in all_names:
            if count >= limit:
                break
            count += 1

            sample_titles = [
                row[0]
                for row in conn.execute(
                    "SELECT title FROM jobs WHERE company = ? LIMIT 10", (comp_name,)
                ).fetchall()
            ]

            slugs = generate_slugs(comp_name)

            for slug in slugs:
                if slug in existing_slugs:
                    continue

                # Probe Greenhouse
                time.sleep(delay_s)
                gh_status, gh_ok, b_name = probe_greenhouse(client, slug, comp_name)
                conn.execute(
                    "INSERT INTO company_probes(name, slug, ats, http_status, checked_at) VALUES (?, ?, ?, ?, ?)",
                    (comp_name, slug, "greenhouse", gh_status, now_iso),
                )
                if gh_ok:
                    hit = {
                        "name": b_name or comp_name,
                        "ats_type": "greenhouse",
                        "ats_slug": slug,
                        "watchlist": True,
                    }
                    discovered.append(hit)
                    existing_slugs.add(slug)
                    break

                # Probe Lever
                time.sleep(delay_s)
                lev_status, lev_ok = probe_lever(client, slug, sample_titles)
                conn.execute(
                    "INSERT INTO company_probes(name, slug, ats, http_status, checked_at) VALUES (?, ?, ?, ?, ?)",
                    (comp_name, slug, "lever", lev_status, now_iso),
                )
                if lev_ok:
                    hit = {
                        "name": comp_name,
                        "ats_type": "lever",
                        "ats_slug": slug,
                        "watchlist": True,
                    }
                    discovered.append(hit)
                    existing_slugs.add(slug)
                    break

                # Probe Ashby
                time.sleep(delay_s)
                ash_status, ash_ok = probe_ashby(client, slug, sample_titles)
                conn.execute(
                    "INSERT INTO company_probes(name, slug, ats, http_status, checked_at) VALUES (?, ?, ?, ?, ?)",
                    (comp_name, slug, "ashby", ash_status, now_iso),
                )
                if ash_ok:
                    hit = {
                        "name": comp_name,
                        "ats_type": "ashby",
                        "ats_slug": slug,
                        "watchlist": True,
                    }
                    discovered.append(hit)
                    existing_slugs.add(slug)
                    break

            conn.commit()

        # Append hits to companies.yaml
        if discovered and target_yaml.exists():
            today_str = now_utc.strftime("%Y-%m-%d")
            with open(target_yaml, "a", encoding="utf-8") as f:
                f.write(f"\n  # discovered {today_str}\n")
                for hit in discovered:
                    f.write(
                        f'  - {{name: "{hit["name"]}", ats_type: {hit["ats_type"]}, ats_slug: {hit["ats_slug"]}, watchlist: true}}\n'
                    )

    finally:
        if http_client is None:
            client.close()

    return discovered
