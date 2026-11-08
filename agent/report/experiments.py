"""Resume A/B tests: balanced random assignment and a cautious comparison (ranges, not winners, until
both arms have enough applications)."""

from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass

RESPONDED = ("assessment", "interview", "offer", "rejected")
MIN_PER_ARM = 30


def assign_variant(conn: sqlite3.Connection, name: str, variants: tuple[str, ...] = ("A", "B")) -> str:
    counts = dict(
        conn.execute(
            "SELECT variant, count(*) FROM experiments WHERE name = ? GROUP BY variant", (name,)
        ).fetchall()
    )
    fewest = min(counts.get(v, 0) for v in variants)
    return random.choice([v for v in variants if counts.get(v, 0) == fewest])


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Arm:
    variant: str
    n: int
    responses: int
    low: float
    high: float


def compare(conn: sqlite3.Connection, name: str = "summary") -> tuple[list[Arm], str]:
    marks = ",".join("?" * len(RESPONDED))
    rows = conn.execute(
        f"SELECT e.variant, count(*) AS n, sum(a.status IN ({marks})) AS r FROM experiments e"
        f" JOIN applications a ON a.id = e.application_id WHERE e.name = ? AND a.submitted_at IS NOT NULL"
        f" GROUP BY e.variant ORDER BY e.variant",
        (*RESPONDED, name),
    ).fetchall()
    arms = [Arm(r["variant"], r["n"], r["r"] or 0, *wilson(r["r"] or 0, r["n"])) for r in rows]
    if len(arms) < 2 or min(a.n for a in arms) < MIN_PER_ARM:
        return arms, "not enough data yet (need 30 submitted per variant)"
    a, b = arms[0], arms[1]
    if a.low > b.high:
        return arms, f"variant {a.variant} gets more responses"
    if b.low > a.high:
        return arms, f"variant {b.variant} gets more responses"
    return arms, "no clear difference yet"
