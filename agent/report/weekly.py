"""Weekly review: what is working, a skill-gap list and one suggested change. Full report as a local
Markdown file; a 6-line summary for WhatsApp (counts and source names only)."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from agent import db, paths
from agent.report import experiments

RESPONDED = ("assessment", "interview", "offer", "rejected")


def _rate(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def by_source(conn: sqlite3.Connection, since: str) -> list[tuple[str, int, int, int]]:
    marks = ",".join("?" * len(RESPONDED))
    rows = conn.execute(
        f"SELECT j.source, count(*) n, sum(a.status IN ({marks})) r, sum(a.status IN ('interview', 'offer')) i"
        f" FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.submitted_at >= ? GROUP BY j.source"
        f" ORDER BY n DESC",
        (*RESPONDED, since),
    ).fetchall()
    return [(r["source"], r["n"], r["r"] or 0, r["i"] or 0) for r in rows]


def by_band(conn: sqlite3.Connection, since: str) -> list[tuple[str, int, int]]:
    out = []
    for low, high in ((0, 60), (60, 70), (70, 80), (80, 101)):
        row = conn.execute(
            "SELECT count(*) n, sum(a.status IN ('interview', 'offer')) i FROM applications a"
            " JOIN scores s ON s.job_id = a.job_id WHERE a.submitted_at >= ? AND s.fit >= ? AND s.fit < ?",
            (since, low, high),
        ).fetchone()
        out.append((f"{low}-{min(high, 100)}", row["n"], row["i"] or 0))
    return out


def skill_gaps(conn: sqlite3.Connection, since: str, top: int = 8) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for row in conn.execute(
        "SELECT s.gaps_json FROM scores s JOIN jobs j ON j.id = s.job_id WHERE j.first_seen_at >= ? AND s.fit >= 55",
        (since,),
    ):
        for gap in json.loads(row["gaps_json"] or "[]"):
            counter[gap.lower()[:60]] += 1
    return counter.most_common(top)


def suggestion(sources: list[tuple[str, int, int, int]], gaps: list[tuple[str, int]], pending: int) -> str:
    ats = [s for s in sources if s[0] in ("greenhouse", "lever", "ashby")]
    boards = [s for s in sources if s[0] not in ("greenhouse", "lever", "ashby", "manual")]
    ats_rate = sum(s[2] for s in ats) / max(1, sum(s[1] for s in ats))
    board_rate = sum(s[2] for s in boards) / max(1, sum(s[1] for s in boards))
    if sum(s[1] for s in ats) >= 10 and ats_rate > board_rate * 1.5:
        return f"ATS-direct replies {ats_rate:.0%} vs boards {board_rate:.0%}: add more companies to companies.yaml."
    if pending >= 3:
        return (
            f"Answer the {pending} pending screening questions (agent answers pending) to unlock more forms."
        )
    if gaps:
        return f"Most common gap in good matches: {gaps[0][0]}. A small project with it would raise your fit."
    return "Keep going: not enough outcomes yet to suggest a change."


def build(conn: sqlite3.Connection, now: datetime) -> tuple[str, str]:
    week = db.to_iso(now - timedelta(days=7))
    month = db.to_iso(now - timedelta(days=30))
    found = conn.execute("SELECT count(*) FROM jobs WHERE first_seen_at >= ?", (week,)).fetchone()[0]
    applied = conn.execute("SELECT count(*) FROM applications WHERE submitted_at >= ?", (week,)).fetchone()[0]
    statuses = dict(
        conn.execute(
            "SELECT status, count(*) FROM applications WHERE submitted_at >= ? GROUP BY status", (month,)
        ).fetchall()
    )
    sent30 = sum(v for k, v in statuses.items() if k not in ("prepared", "skipped"))
    responded = sum(statuses.get(k, 0) for k in RESPONDED)
    interviews = statuses.get("interview", 0) + statuses.get("offer", 0)
    sources = by_source(conn, month)
    bands = by_band(conn, month)
    gaps = skill_gaps(conn, month)
    pending = conn.execute("SELECT count(*) FROM pending_questions").fetchone()[0]
    unsure = conn.execute(
        "SELECT count(*) FROM events WHERE occurred_at >= ? AND details_json LIKE '%\"unsure\": true%'",
        (week,),
    ).fetchone()[0]
    llm = conn.execute(
        "SELECT stage, count(*) n, sum(ok) ok FROM llm_calls WHERE created_at >= ? AND sent = 1 GROUP BY stage",
        (week,),
    ).fetchall()
    health = conn.execute(
        "SELECT stage, count(*) n, sum(ok) ok FROM runs WHERE started_at >= ? GROUP BY stage", (week,)
    ).fetchall()
    arms, verdict = experiments.compare(conn)
    tip = suggestion(sources, gaps, pending)

    md = [f"# Weekly review — {now:%d %b %Y}", "", "## Outcome", "",
          f"- Jobs found this week: {found}", f"- Applications this week: {applied}",
          f"- Last 30 days: {sent30} applied, response rate {_rate(responded, sent30)}, "
          f"interview rate {_rate(interviews, sent30)}", "", "## By source (30 days)", "",
          "| source | applied | responses | interviews |", "|---|---|---|---|"]  # fmt: skip
    md += [f"| {s} | {n} | {r} ({_rate(r, n)}) | {i} ({_rate(i, n)}) |" for s, n, r, i in sources]
    md += ["", "## By fit band (30 days)", "", "| fit | applied | interviews |", "|---|---|---|"]
    md += [f"| {b} | {n} | {i} ({_rate(i, n)}) |" for b, n, i in bands]
    md += ["", "## Skill gaps in good matches", ""]
    md += [f"- {g} ({c} jobs)" for g, c in gaps] or ["- none recorded"]
    md += ["", "## Resume A/B (summary)", ""]
    md += [f"- {a.variant}: {a.responses}/{a.n} responded (range {a.low:.0%}–{a.high:.0%})" for a in arms]
    md += [f"- {verdict}", "", "## Needs you", "",
           f"- Pending screening questions: {pending} (`agent answers pending`)",
           f"- Emails filed as unsure this week: {unsure}", "", "## Pipeline health (7 days)", ""]  # fmt: skip
    md += [f"- {h['stage']}: {h['ok'] or 0}/{h['n']} runs ok" for h in health]
    md += ["", "## LLM requests (7 days)", ""]
    md += [f"- {r['stage']}: {r['n']} ({r['ok'] or 0} ok)" for r in llm] or ["- none"]
    md += ["", "## Suggested change", "", tip, ""]

    top = ", ".join(f"{s} {_rate(r, n)}" for s, n, r, _ in sources[:3]) or "no data"
    text = "\n".join([
        f"📊 *Weekly review* {now:%d %b}",
        f"Found {found} · applied {applied} this week",
        f"30d: response {_rate(responded, sent30)} · interviews {_rate(interviews, sent30)}",
        f"Response by source: {top}",
        f"Needs you: {pending} questions · {unsure} unsure emails",
        f"Tip: {tip}",
    ])  # fmt: skip
    return "\n".join(md), text


def write(conn: sqlite3.Connection, now: datetime) -> tuple[Path, str]:
    md, text = build(conn, now)
    out = paths.output_dir() / "reports"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"weekly-{now:%Y-%m-%d}.md"
    path.write_text(md, encoding="utf-8")
    return path, text
