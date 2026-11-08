"""Scoring stage. fit (0-100) is the weighted sum from the build plan; priority adds source and watchlist.

    fit = 0.35 semantic + 0.25 must-have coverage + 0.20 llm rubric + 0.10 seniority + 0.10 freshness

When the rubric is not available yet (quota, not in the top N), its weight is shared out over the
other parts, and the rubric is added later by a following run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent import db, paths
from agent.config import Profile, ScoringWeights, Search, Settings
from agent.discover.base import norm
from agent.llm import LLMBusy, LLMError, LLMPaused, QuotaExhausted
from agent.match import filters, rubric, similarity, skills
from agent.runs import stage_run

log = logging.getLogger(__name__)

JUNIOR_WORDS = {
    "intern",
    "internship",
    "junior",
    "jr",
    "entry",
    "graduate",
    "trainee",
    "associate",
    "fresher",
    "i",
}


@dataclass
class Parts:
    semantic: float
    skills: float
    seniority: float
    freshness: float
    llm: float | None = None


def refit_path() -> Path:
    return paths.agent_home() / "data" / "weights.json"


def weights(settings: Settings) -> ScoringWeights:
    if settings.scoring.use_refit_weights and refit_path().exists():
        try:
            return ScoringWeights.model_validate_json(refit_path().read_text(encoding="utf-8"))
        except ValueError:
            log.warning("weights.json invalid; using settings weights")
    return settings.scoring.weights


def fit(parts: Parts, w: ScoringWeights) -> float:
    items = [(w.semantic, parts.semantic), (w.skills, parts.skills), (w.seniority, parts.seniority),
             (w.freshness, parts.freshness)]  # fmt: skip
    if parts.llm is not None:
        items.append((w.llm, parts.llm))
    total = sum(weight for weight, _ in items)
    return round(100 * sum(weight * value for weight, value in items) / total, 1) if total else 0.0


def freshness(posted: datetime | None, now: datetime, horizon_days: int = 30) -> float:
    """1.0 if posted within 24 h, falling linearly to 0 at `horizon_days` (the max posting age)."""
    if posted is None:
        return 0.5
    age_h = (now - posted).total_seconds() / 3600
    if age_h <= 24:
        return 1.0
    return max(0.0, 1 - (age_h - 24) / (max(2, horizon_days) * 24 - 24))


def seniority(title: str, rubric_fit: str | None = None) -> float:
    if rubric_fit == "above":
        return 0.1
    words = set(norm(title).split())
    if words & JUNIOR_WORDS:
        return 1.0
    return 1.0 if rubric_fit == "match" else 0.7


def priority(fit_value: float, source: str, watchlist: bool, settings: Settings) -> float:
    weight = settings.scoring.source_weights.get(source, 0.7)
    bonus = settings.scoring.watchlist_bonus if watchlist else 0.0
    return round(fit_value * weight * (1 + bonus), 1)


def _posted(job) -> datetime | None:
    value = job["posted_at"] or job["first_seen_at"]
    return db.parse_iso(value) if value else None


def _watchlisted(conn: sqlite3.Connection, job) -> bool:
    if job["company_id"] is None:
        return False
    row = conn.execute("SELECT watchlist FROM companies WHERE id = ?", (job["company_id"],)).fetchone()
    return bool(row and row["watchlist"])


# Bump when the formula or calibration changes: stored scores are recomputed once, without API calls.
SCORING_VERSION = "4"
SCORING_FLAG = "scoring_version"


def rescore(
    conn: sqlite3.Connection, settings: Settings, search: Search, profile: Profile, now: datetime
) -> int:
    """Recompute fit and priority for stored scores from cached embeddings and saved rubric results."""
    w = weights(settings)
    model = settings.llm.embedding_model
    pvec = similarity.get_cached(conn, similarity.profile_key(profile), model)
    rows = conn.execute("SELECT j.*, s.semantic, s.skills, s.llm_rubric, s.model AS score_model, s.seniority"
                        " AS sen FROM jobs j JOIN scores s ON s.job_id = j.id").fetchall()  # fmt: skip
    with db.transaction(conn):
        for job in rows:
            sem = job["semantic"]
            jvec = similarity.get_cached(conn, f"job:{job['id']}", model)
            if pvec and jvec:
                sem = similarity.scale_embedding(
                    similarity.cosine(pvec, jvec), scale=settings.llm.embedding_scale
                )
            parts = Parts(sem, job["skills"], job["sen"], freshness(_posted(job), now, search.max_posting_age_days),
                          job["llm_rubric"])  # fmt: skip
            value = fit(parts, w)
            conn.execute(
                "UPDATE scores SET semantic = ?, freshness = ?, fit = ?, priority = ? WHERE job_id = ?",
                (
                    sem,
                    parts.freshness,
                    value,
                    priority(value, job["source"], _watchlisted(conn, job), settings),
                    job["id"],
                ),
            )
    return len(rows)


def upgrade_semantic(conn, client, settings: Settings, search: Search, profile: Profile, now: datetime,
                     run_id=None, limit: int = 50) -> int:  # fmt: skip
    model = settings.llm.embedding_model
    rows = conn.execute(
        "SELECT j.* FROM jobs j JOIN scores s ON s.job_id = j.id LEFT JOIN embeddings e"
        " ON e.key = 'job:' || j.id AND e.model = ? WHERE e.key IS NULL ORDER BY s.priority DESC LIMIT ?",
        (model, limit),
    ).fetchall()
    if not rows or similarity.get_cached(conn, similarity.profile_key(profile), model) is None:
        return 0
    try:
        similarity.embed_missing(conn, client, rows, {}, run_id, max_wait_s=0)
    except Exception as exc:  # quota: try again next run
        log.info("semantic upgrade postponed: %s", type(exc).__name__)
    rescore(conn, settings, search, profile, now)
    return len(rows)


@dataclass
class ScoreResult:
    filtered: dict[str, int]
    scored: int
    rubric_done: int
    semantic_method: str
    rubric_stopped: str | None


def run_scoring(
    conn: sqlite3.Connection,
    settings: Settings,
    search: Search,
    profile: Profile,
    client=None,
    now: datetime | None = None,
) -> ScoreResult:
    now = now or datetime.now(UTC)
    w = weights(settings)
    if db.get_flag(conn, SCORING_FLAG) != SCORING_VERSION:
        rescore(conn, settings, search, profile, now)
        db.set_flag(conn, SCORING_FLAG, SCORING_VERSION)
    with stage_run(conn, "score") as run:
        counts = filters.run_filters(conn, search, now)

        # 1. Cheap parts for every passed job that has no score yet.
        todo = conn.execute(
            "SELECT j.* FROM jobs j LEFT JOIN scores s ON s.job_id = j.id"
            " WHERE j.filtered_at IS NOT NULL AND j.filter_reason IS NULL AND s.job_id IS NULL"
        ).fetchall()
        method = "none"
        if todo:
            semantic, method = similarity.semantic_scores(conn, client, profile, todo, run.id)
            with db.transaction(conn):
                for job, sem in zip(todo, semantic, strict=True):
                    cov, found, gaps = skills.coverage(f"{job['title']}\n{job['description'] or ''}", profile)
                    parts = Parts(
                        sem,
                        cov,
                        seniority(job["title"]),
                        freshness(_posted(job), now, search.max_posting_age_days),
                    )
                    value = fit(parts, w)
                    conn.execute(
                        "INSERT INTO scores(job_id, fit, semantic, skills, llm_rubric, seniority, freshness,"
                        " reasons_json, gaps_json, skills_found_json, model, scored_at, priority)"
                        " VALUES (?, ?, ?, ?, NULL, ?, ?, '[]', ?, ?, ?, ?, ?)",
                        (
                            job["id"], value, parts.semantic, parts.skills, parts.seniority, parts.freshness,
                            json.dumps(sorted(gaps)), json.dumps(sorted(found)), method, db.to_iso(now),
                            priority(value, job["source"], _watchlisted(conn, job), settings),
                        ),
                    )  # fmt: skip

        # 1b. Jobs scored with the local fallback get real embeddings when quota allows (50 per run).
        if client is not None:
            upgrade_semantic(conn, client, settings, search, profile, now, run.id)

        # 2. LLM rubric for the best jobs that don't have one yet.
        rubric_done, stopped = 0, None
        if client is not None and settings.scoring.rubric_per_run:
            best = conn.execute(
                "SELECT j.*, s.semantic, s.skills, s.freshness FROM jobs j JOIN scores s ON s.job_id = j.id"
                " WHERE s.llm_rubric IS NULL AND s.fit >= ? ORDER BY s.priority DESC LIMIT ?",
                (settings.scoring.rubric_min_prescore, settings.scoring.rubric_per_run),
            ).fetchall()
            for job in best:
                try:
                    result, model = rubric.rate(client, profile, job, run.id)
                except (LLMPaused, QuotaExhausted, LLMBusy) as exc:
                    stopped = type(exc).__name__
                    break
                except (LLMError, ValueError) as exc:
                    log.warning("rubric failed for job %s: %s", job["id"], type(exc).__name__)
                    continue
                parts = Parts(
                    job["semantic"], job["skills"], seniority(job["title"], result.seniority_fit),
                    freshness(_posted(job), now, search.max_posting_age_days), result.score / 10,
                )  # fmt: skip
                value = fit(parts, w)
                conn.execute(
                    "UPDATE scores SET llm_rubric = ?, seniority = ?, fit = ?, reasons_json = ?, gaps_json = ?,"
                    " model = ?, scored_at = ?, priority = ? WHERE job_id = ?",
                    (
                        parts.llm, parts.seniority, value, json.dumps(result.reasons),
                        json.dumps(result.gaps), model, db.to_iso(now),
                        priority(value, job["source"], _watchlisted(conn, job), settings), job["id"],
                    ),
                )  # fmt: skip
                rubric_done += 1
        run.items = len(todo)
    return ScoreResult(counts, len(todo), rubric_done, method, stopped)
