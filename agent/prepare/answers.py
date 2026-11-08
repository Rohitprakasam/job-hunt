"""Answer bank for screening questions.

- Every answer is stored with the normalised question, so it is reused word for word.
- Legal questions (authorization, sponsorship, disability, criminal record, veteran, demographics)
  only ever use answers saved by you or derived from your own profile.yaml; never the LLM.
- Other new questions get an LLM answer only when it follows directly from the profile; otherwise
  the question goes on the pending list and the form is skipped.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from agent import db
from agent.config import Profile

LEGAL_RE = re.compile(
    r"authori[sz]ed to work|work authori[sz]ation|right to work|eligib\w* to work|sponsor|visa|work permit|"
    r"disabilit|criminal|convict|felony|background check|veteran|military|gender|sex\b|race\b|ethnic|"
    r"pronoun|sexual orientation|transgender|hispanic|latino|citizenship|citizen\b",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(
    r"\b(please|kindly|enter|provide|your|the|a|an)\b|\(required\)|\(optional\)|\*", re.IGNORECASE
)
YES = {"yes", "y", "true"}
NO = {"no", "n", "false"}


def normalize(question: str) -> str:
    text = _FILLER_RE.sub(" ", question.lower())
    return re.sub(r"[^a-z0-9+#]+", " ", text).strip()


def category(question: str) -> str:
    return "legal" if LEGAL_RE.search(question) else "factual"


def _months(start: str, end: str | None) -> int:
    y1, m1 = map(int, start.split("-"))
    if end:
        y2, m2 = map(int, end.split("-"))
    else:
        today = date.today()
        y2, m2 = today.year, today.month
    return max(0, (y2 - y1) * 12 + m2 - m1 + 1)


def years_of_experience(profile: Profile) -> str:
    months = sum(_months(e.start, e.end) for e in profile.experience)
    years = months / 12
    return (
        "0" if months == 0 else (f"{years:.1f}".rstrip("0").rstrip(".") if years < 1 else str(round(years)))
    )


def profile_answers(profile: Profile) -> list[tuple[str, str, str]]:
    """(question, answer, category) facts that come straight from profile.yaml."""
    c = profile.contact
    link = {link.label.lower(): link.url for link in c.links}
    out = [
        ("First name", c.first_name, "factual"),
        ("Last name", c.last_name, "factual"),
        ("Full name", c.full_name, "factual"),
        ("Name", c.full_name, "factual"),
        ("Email", c.email, "factual"),
        ("Email address", c.email, "factual"),
        ("Phone", c.phone_e164, "factual"),
        ("Phone number", c.phone_e164, "factual"),
        ("Mobile number", c.phone_e164, "factual"),
        ("Country", c.country, "factual"),
        ("Notice period", f"{profile.notice_period_days} days" if profile.notice_period_days else "Immediate", "factual"),
        ("Notice period in days", str(profile.notice_period_days), "factual"),
        ("How soon can you join", "Immediately" if profile.notice_period_days == 0 else f"{profile.notice_period_days} days", "factual"),
        ("Total years of experience", years_of_experience(profile), "factual"),
        ("Years of experience", years_of_experience(profile), "factual"),
    ]  # fmt: skip
    if c.city:
        out += [("City", c.city, "factual"), ("Current location", f"{c.city}, {c.country}", "factual"),
                ("Location", f"{c.city}, {c.country}", "factual")]  # fmt: skip
    for label in ("linkedin", "github"):
        if label in link:
            out += [(f"{label.title()} profile", link[label], "factual"), (f"{label.title()} URL", link[label], "factual"),
                    (label.title(), link[label], "factual")]  # fmt: skip
    website = next((u for k, u in link.items() if k in ("portfolio", "website", "personal website")), None)
    if website:
        out += [("Website", website, "factual"), ("Portfolio", website, "factual")]
    s = profile.salary_expectation
    if s.target:
        out += [("Expected salary", f"{s.target} {s.currency} per year", "factual"),
                ("Expected CTC", str(s.target), "factual"), ("Salary expectations", f"{s.target} {s.currency}", "factual")]  # fmt: skip
    if profile.graduation:
        g = profile.graduation
        out += [("Graduation year", g.date[:4], "factual"), ("Highest degree", g.degree, "factual"),
                ("University", g.institution, "factual"), ("School", g.institution, "factual"),
                ("Graduation date", g.date, "factual")]  # fmt: skip
    if profile.experience:
        latest = profile.experience[0]
        out += [("Current or most recent employer", latest.company, "factual"),
                ("Current company", latest.company if latest.end is None else "None", "factual"),
                ("Most recent job title", latest.title, "factual")]  # fmt: skip
    auth = profile.work_authorization
    for country in auth.countries:
        out.append((f"Are you legally authorized to work in {country}", "Yes", "legal"))
    out += [
        ("Will you now or in the future require sponsorship for employment visa status", "Yes" if auth.needs_sponsorship else "No", "legal"),
        ("Do you require visa sponsorship", "Yes" if auth.needs_sponsorship else "No", "legal"),
    ]  # fmt: skip
    return out


def upsert(
    conn: sqlite3.Connection, question: str, answer: str, cat: str, approved: bool, overwrite: bool = True
) -> None:
    sql = (
        "INSERT INTO answers_bank(question_norm, question_raw, answer, category, approved, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(question_norm) DO "
    )
    sql += (
        "UPDATE SET answer = excluded.answer, category = excluded.category, approved = excluded.approved,"
        " updated_at = excluded.updated_at"
        if overwrite
        else "NOTHING"
    )
    conn.execute(sql, (normalize(question), question, answer, cat, int(approved), db.utcnow()))
    conn.execute("DELETE FROM pending_questions WHERE question_norm = ?", (normalize(question),))


PROFILE_MARK = "[profile] "


def seed(conn: sqlite3.Connection, profile: Profile) -> int:
    """(Re)write the profile-derived answers. Answers you set by hand are never overwritten."""
    count = 0
    for question, answer, cat in profile_answers(profile):
        key = normalize(question)
        existing = conn.execute(
            "SELECT question_raw FROM answers_bank WHERE question_norm = ?", (key,)
        ).fetchone()
        if existing and not existing["question_raw"].startswith(PROFILE_MARK):
            continue
        conn.execute(
            "INSERT INTO answers_bank(question_norm, question_raw, answer, category, approved, updated_at)"
            " VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT(question_norm) DO UPDATE SET answer = excluded.answer,"
            " category = excluded.category, approved = 1, updated_at = excluded.updated_at",
            (key, PROFILE_MARK + question, answer, cat, db.utcnow()),
        )
        count += 1
    return count


@dataclass
class Answer:
    value: str
    source: str  # bank | llm
    category: str


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def match_option(answer: str, options: list[str]) -> str | None:
    """Map a stored answer onto one of a select's options."""
    if not options:
        return answer
    low = answer.strip().lower()
    for option in options:
        if option.strip().lower() == low:
            return option
    for option in options:
        o = option.strip().lower()
        if (low in YES and o.startswith("yes")) or (low in NO and o.startswith("no")):
            return option
    for option in options:
        if option.strip().lower().startswith(low) or low.startswith(option.strip().lower()):
            return option
    return None


_SPONSOR_RE = re.compile(r"sponsor|work permit|visa", re.IGNORECASE)
_AUTHORIZED_RE = re.compile(
    r"authori[sz]ed to work|eligib\w* to work|right to work|legally (able|permitted)", re.IGNORECASE
)


def _legal_intent(rows: list[sqlite3.Row], question: str) -> sqlite3.Row | None:
    """Same legal question, different wording: match by intent, but only to saved (approved) answers.
    Work authorization must also name the same country as the saved answer."""
    approved = [r for r in rows if r["category"] == "legal" and r["approved"]]
    if _SPONSOR_RE.search(question):
        return next((r for r in approved if _SPONSOR_RE.search(r["question_raw"])), None)
    if _AUTHORIZED_RE.search(question):
        low = question.lower()
        for row in approved:
            match = re.search(r"authori[sz]ed to work in (.+)$", row["question_raw"].lower())
            if match and match.group(1).strip(" ?.") in low:
                return row
    return None


def rule_answer(question: str, profile: Profile, company: str | None = None) -> str | None:
    """Plain facts that follow from profile.yaml for common differently-worded questions."""
    q = question.lower()
    if re.search(r"previously (worked|been employed|been an employee|consulted|interned)", q) and company:
        worked = {e.company.lower() for e in profile.experience}
        return "Yes" if any(company.lower() in w or w in company.lower() for w in worked) else "No"
    if re.search(
        r"country of residence|current country|country (where|in which) you (live|reside|are located)", q
    ):
        return profile.contact.country
    if profile.contact.city and re.search(
        r"current city|city (where|in which) you (live|reside|are located)|primary residence|city .*residen",
        q,
    ):
        return profile.contact.city
    if re.search(r"current (job )?title", q) and profile.experience:
        latest = profile.experience[0]
        return latest.title if latest.end is None else None
    if re.search(r"preferred (first )?name|name you('d| would)? prefer", q):
        return profile.contact.first_name
    return None


def lookup(conn: sqlite3.Connection, question: str, options: list[str] | None = None) -> Answer | None:
    key = normalize(question)
    cat = category(question)
    rows = conn.execute(
        "SELECT * FROM answers_bank WHERE category = ? OR ? = 'factual'", (cat, cat)
    ).fetchall()
    best, best_score = None, 0.0
    for row in rows:
        if row["category"] == "legal" and cat != "legal":
            continue
        if cat == "legal" and not row["approved"]:
            continue  # legal answers must be ones you saved
        score = 1.0 if row["question_norm"] == key else _jaccard(row["question_norm"], key)
        if score > best_score:
            best, best_score = row, score
    if (best is None or best_score < 0.85) and cat == "legal":
        best, best_score = _legal_intent(rows, question), 1.0
    if best is None or best_score < (0.75 if cat == "factual" else 0.85):
        return None
    value = match_option(best["answer"], options or [])
    if value is None:
        return None
    conn.execute("UPDATE answers_bank SET used_count = used_count + 1 WHERE id = ?", (best["id"],))
    return Answer(value, "bank", best["category"])


class Derived(BaseModel):
    derivable: bool
    answer: str = Field(max_length=500)
    confidence: float = Field(ge=0, le=1)


SYSTEM = (
    "Answer one job-application form question for the candidate using ONLY facts in the profile. "
    "If the answer does not follow directly from the profile (opinion, preference, legal status, anything "
    "not stated), set derivable=false and answer=''. If options are given, answer with one option exactly. "
    "Keep answers short and factual. Reply with JSON only."
)


def derive(client, profile: Profile, question: str, options: list[str] | None = None) -> Answer | None:
    if category(question) == "legal":
        return None
    from agent.match.similarity import profile_text

    content = f"PROFILE\n{profile_text(profile)[:5000]}\n\nQUESTION: {question}"
    if options:
        content += "\nOPTIONS: " + " | ".join(options)
    result = client.complete(
        "answer", "strong", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
        max_tokens=1000, json_schema=Derived,
    )  # fmt: skip
    data: Derived | None = result.data  # type: ignore[assignment]
    if data is None or not data.derivable or data.confidence < 0.85 or not data.answer.strip():
        return None
    value = match_option(data.answer, options or [])
    return Answer(value, "llm", "factual") if value else None


def record_pending(conn: sqlite3.Connection, question: str, job_id: int | None) -> None:
    now = db.utcnow()
    conn.execute(
        "INSERT INTO pending_questions(question_norm, question_raw, example_job_id, first_seen_at, last_seen_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(question_norm) DO UPDATE SET seen_count = seen_count + 1,"
        " last_seen_at = excluded.last_seen_at",
        (normalize(question), question, job_id, now, now),
    )
