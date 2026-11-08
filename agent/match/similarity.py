"""Resume-vs-job similarity: Gemini embeddings (cached) with a local TF-IDF fallback."""

from __future__ import annotations

import array
import hashlib
import logging
import math
import re
import sqlite3
import time
from collections import Counter

from agent import db
from agent.config import Profile

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z][a-z0-9+#.]{1,}")
_STOP = set(
    "the and for with you your our are will this that from have has can all any not but who what into "
    "their they them about such using use able work working team teams role job new other more must "
    "including include etc also within across well good strong".split()
)


def profile_text(profile: Profile) -> str:
    """Job-relevant resume text only (no contact block): summary, skills, titles, bullets."""
    parts = [profile.summary]
    parts += [f"{k}: {', '.join(v)}" for k, v in profile.skills.items()]
    for e in profile.experience:
        parts.append(e.title)
        parts += [b.text for b in e.bullets]
    for p in profile.projects:
        parts.append(p.name)
        parts += [b.text for b in p.bullets]
    parts += [c.name for c in profile.certifications]
    return "\n".join(parts)


def job_text(job: sqlite3.Row | dict) -> str:
    return f"{job['title']}\n{(job['description'] or '')[:3000]}"


# ---------------------------------------------------------------- vectors


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> list[float]:
    out = array.array("f")
    out.frombytes(blob)
    return out.tolist()


def get_cached(conn: sqlite3.Connection, key: str, model: str) -> list[float] | None:
    row = conn.execute("SELECT vector FROM embeddings WHERE key = ? AND model = ?", (key, model)).fetchone()
    return _unpack(row["vector"]) if row else None


def put_cached(conn: sqlite3.Connection, key: str, model: str, vector: list[float]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO embeddings(key, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)",
        (key, model, len(vector), _pack(vector), db.utcnow()),
    )


def profile_key(profile: Profile) -> str:
    return "profile:" + hashlib.sha256(profile_text(profile).encode()).hexdigest()[:16]


# Typical cosine ranges -> 0..1 (rough calibration so the parts are comparable in the weighted sum).
def scale_embedding(cos: float, scale: tuple[float, float] = (0.50, 0.70)) -> float:
    # default (0.50, 0.70): unrelated ~0.50, close match ~0.70.
    low, high = scale
    span = high - low
    if span <= 0:
        return 0.0
    return min(1.0, max(0.0, (cos - low) / span))


def scale_tfidf(cos: float) -> float:
    return min(1.0, max(0.0, cos / 0.15))


# ---------------------------------------------------------------- local fallback


def _terms(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP]


def tfidf_scores(profile_doc: str, job_docs: list[str]) -> list[float]:
    """Cosine of TF-IDF vectors, IDF computed over the profile plus this batch of jobs."""
    docs = [Counter(_terms(profile_doc))] + [Counter(_terms(d)) for d in job_docs]
    n = len(docs)
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(doc.keys())
    idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}

    def vec(doc: Counter[str]) -> dict[str, float]:
        return {t: (1 + math.log(c)) * idf[t] for t, c in doc.items()}

    base = vec(docs[0])
    base_norm = math.sqrt(sum(v * v for v in base.values()))
    out = []
    for doc in docs[1:]:
        v = vec(doc)
        dot = sum(w * base.get(t, 0.0) for t, w in v.items())
        norm = math.sqrt(sum(w * w for w in v.values()))
        out.append(dot / (norm * base_norm) if norm and base_norm else 0.0)
    return out


def embed_missing(
    conn, client, jobs: list, vectors: dict, run_id=None, sleep=time.sleep, max_wait_s: int = 360
) -> None:
    """Embed jobs in quota-sized chunks, caching each chunk, waiting out per-minute limits
    (up to `max_wait_s` in total). Raises if the daily quota is gone or the wait runs out."""
    from agent.llm import LLMBusy

    model = client.llm.embedding_model
    chunk = max(
        1, min(client.llm.embedding_batch, int(client.llm.embedding_limit.rpm * client.llm.quota_headroom))
    )
    waited = 0
    todo = list(jobs)
    while todo:
        part = todo[:chunk]
        try:
            fresh = client.embed("embed_jobs", [job_text(j) for j in part], run_id)
        except LLMBusy:
            if waited >= max_wait_s:
                raise
            sleep(20)
            waited += 20
            continue
        for job, vector in zip(part, fresh, strict=True):
            put_cached(conn, f"job:{job['id']}", model, vector)
            vectors[job["id"]] = vector
        todo = todo[chunk:]


def semantic_scores(
    conn, client, profile: Profile, jobs: list, run_id: int | None = None, sleep=time.sleep
) -> tuple[list[float], str]:
    """Scores 0..1 for each job and the method used ('embedding' or 'tfidf')."""
    if client is not None:
        model = client.llm.embedding_model
        try:
            pkey = profile_key(profile)
            pvec = get_cached(conn, pkey, model)
            if pvec is None:
                pvec = client.embed("embed_profile", [profile_text(profile)], run_id)[0]
                put_cached(conn, pkey, model, pvec)
            vectors: dict[int, list[float]] = {}
            missing = []
            for job in jobs:
                cached = get_cached(conn, f"job:{job['id']}", model)
                if cached is None:
                    missing.append(job)
                else:
                    vectors[job["id"]] = cached
            embed_missing(conn, client, missing, vectors, run_id, sleep)
            scale = (
                client.llm.embedding_scale
                if hasattr(client, "llm") and hasattr(client.llm, "embedding_scale")
                else (0.50, 0.70)
            )
            return [scale_embedding(cosine(pvec, vectors[j["id"]]), scale=scale) for j in jobs], "embedding"
        except Exception as exc:  # quota, network, leak guard: score locally instead of stopping
            log.warning("embeddings unavailable (%s); using local similarity", type(exc).__name__)
    raw = tfidf_scores(profile_text(profile), [job_text(j) for j in jobs])
    return [scale_tfidf(c) for c in raw], "tfidf"
