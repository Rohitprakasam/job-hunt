"""Re-fit the score weights from outcomes: logistic regression of 'got a response' on the score parts.
Pure Python (gradient descent); runs only with at least 50 submitted applications that have an outcome."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from agent.config import ScoringWeights

PARTS = ("semantic", "skills", "llm", "seniority", "freshness")
MIN_SAMPLES = 50
RESPONDED = ("assessment", "interview", "offer", "rejected")
NO_RESPONSE = ("no_response",)


def samples(conn: sqlite3.Connection) -> list[tuple[list[float], int]]:
    rows = conn.execute(
        "SELECT s.semantic, s.skills, coalesce(s.llm_rubric, 0.5) llm, s.seniority, s.freshness, a.status"
        " FROM applications a JOIN scores s ON s.job_id = a.job_id WHERE a.submitted_at IS NOT NULL"
        " AND a.status IN ('assessment', 'interview', 'offer', 'rejected', 'no_response')"
    ).fetchall()
    return [([r[p] or 0.0 for p in PARTS], int(r["status"] in RESPONDED)) for r in rows]


def fit_logistic(
    data: list[tuple[list[float], int]], steps: int = 4000, lr: float = 0.5, l2: float = 0.01
) -> list[float]:
    k = len(PARTS)
    w = [0.0] * (k + 1)  # last = bias
    n = len(data)
    for _ in range(steps):
        grad = [0.0] * (k + 1)
        for x, y in data:
            z = sum(wi * xi for wi, xi in zip(w, x, strict=False)) + w[-1]
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            for i in range(k):
                grad[i] += (p - y) * x[i]
            grad[-1] += p - y
        for i in range(k):
            w[i] -= lr * (grad[i] / n + l2 * w[i])
        w[-1] -= lr * grad[-1] / n
    return w[:k]


def to_weights(coefficients: list[float], floor: float = 0.03) -> ScoringWeights:
    """Positive, normalised weights. Parts with negative pull keep a small floor instead of zero."""
    raw = [max(floor, c) for c in coefficients]
    total = sum(raw)
    return ScoringWeights(**{p: round(v / total, 3) for p, v in zip(PARTS, raw, strict=True)})


def refit(conn: sqlite3.Connection, target: Path) -> tuple[ScoringWeights | None, int]:
    data = samples(conn)
    if len(data) < MIN_SAMPLES or len({y for _, y in data}) < 2:
        return None, len(data)
    weights = to_weights(fit_logistic(data))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(weights.model_dump(), indent=2), encoding="utf-8")
    return weights, len(data)
