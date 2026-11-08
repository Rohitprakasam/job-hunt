"""Classify job-search emails: rules first (high precision), then the fast LLM for the rest.

Only the sender domain, subject and the first 1,500 characters (quoted replies and signatures cut,
contact details redacted by the LLM client) are ever sent to the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["applied", "assessment", "interview", "rejected", "offer", "recruiter_outreach", "other"]

ATS_SENDERS = (
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "hire.lever.co", "ashbyhq.com", "myworkday.com",
    "workday.com", "smartrecruiters.com", "icims.com", "workablemail.com", "workable.com", "jobvite.com",
    "taleo.net", "successfactors.com", "naukri.com", "linkedin.com", "indeed.com", "hirist.com",
    "instahyre.com", "wellfound.com", "hackerrank.com", "hackerearth.com", "codility.com", "testgorilla.com",
    "mettl.com", "imocha.io", "calendly.com",
)  # fmt: skip

# Order matters: the first family that matches wins, strongest outcome first.
RULES: list[tuple[Status, float, tuple[str, ...]]] = [
    ("offer", 0.95, ("offer letter", "pleased to offer", "extend an offer", "offer of employment", "compensation package")),
    ("rejected", 0.95, (
        "not moving forward", "not to move forward", "decided to move forward with other", "other candidates",
        "position has been filled", "will not be proceeding", "won't be moving", "unable to offer you",
        "not be progressing", "regret to inform", "unfortunately, we", "we have decided to pursue",
        "not selected", "no longer under consideration",
    )),
    ("assessment", 0.9, (
        "coding challenge", "online assessment", "take-home", "take home assignment", "hackerrank",
        "codility", "assessment link", "complete the assessment", "technical test", "aptitude test",
        "testgorilla", "hackerearth", "mettl",
    )),
    ("interview", 0.9, (
        "schedule an interview", "interview invitation", "invite you to interview", "invite you for an interview",
        "book a time", "calendly.com", "availability for a call", "phone screen", "next round",
        "interview with", "would like to speak with you", "set up a call",
    )),
    ("applied", 0.95, (
        "thank you for applying", "thanks for applying", "application received", "received your application",
        "we have received your application", "application has been submitted", "thank you for your interest in",
        "your application for", "successfully applied",
    )),
]  # fmt: skip

_QUOTE_RE = re.compile(r"^(>|On .+ wrote:$|-----Original Message-----|From: .+)", re.MULTILINE)
_SIGNATURE_RE = re.compile(
    r"^(--\s*$|Best regards|Kind regards|Regards,|Thanks,|Sincerely,)", re.MULTILINE | re.IGNORECASE
)
_URGENT_HINT = re.compile(r"\b(interview|assessment|offer)\b", re.IGNORECASE)


@dataclass
class Email:
    msg_id: str
    sender: str
    subject: str
    date: datetime
    body: str
    thread_id: str | None = None

    @property
    def sender_domain(self) -> str:
        address = self.sender.split("<")[-1].rstrip(">").strip().lower()
        return address.split("@")[-1] if "@" in address else address


class EmailClass(BaseModel):
    status: Status
    company: str | None = None
    role: str | None = None
    confidence: float = Field(ge=0, le=1)


def trimmed_body(body: str, limit: int = 1500) -> str:
    cut = _QUOTE_RE.search(body or "")
    text = body[: cut.start()] if cut else (body or "")
    sig = _SIGNATURE_RE.search(text)
    text = text[: sig.start()] if sig and sig.start() > 200 else text
    return re.sub(r"\s+", " ", text).strip()[:limit]


def is_relevant(email: Email, applied_domains: set[str], applied_terms: set[str]) -> bool:
    """Narrow the inbox: ATS senders, domains of companies applied to, or subjects naming one."""
    domain = email.sender_domain
    if any(domain == s or domain.endswith("." + s) for s in ATS_SENDERS):
        return True
    if any(domain == d or domain.endswith("." + d) for d in applied_domains if d):
        return True
    subject = email.subject.lower()
    return any(term and term in subject for term in applied_terms)


def by_rules(email: Email) -> EmailClass | None:
    text = f"{email.subject}\n{trimmed_body(email.body, 4000)}".lower()
    for status, confidence, phrases in RULES:
        if any(p in text for p in phrases):
            return EmailClass(status=status, confidence=confidence)
    return None


SYSTEM = (
    "Classify one job-search email. status: applied (confirmation of an application), assessment (test or "
    "coding challenge), interview (invitation or scheduling), rejected, offer, recruiter_outreach (recruiter "
    "writes about a role the candidate did not apply to), other. Extract company and role if stated. "
    "confidence 0-1. Reply with JSON only."
)


def by_llm(client, email: Email) -> EmailClass:
    content = f"Sender domain: {email.sender_domain}\nSubject: {email.subject}\n\n{trimmed_body(email.body)}"
    result = client.complete(
        "classify_email", "fast", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
        max_tokens=1000, json_schema=EmailClass,
    )  # fmt: skip
    if result.data is None:
        raise ValueError("no classification")
    return result.data  # type: ignore[return-value]


def classify(email: Email, client=None) -> EmailClass:
    ruled = by_rules(email)
    if ruled is not None:
        return ruled
    if client is not None:
        try:
            return by_llm(client, email)
        except Exception:  # quota or provider trouble: fall through to the cautious default
            pass
    # Never miss an interview: any mention gets a low-confidence label that still alerts.
    hint = _URGENT_HINT.search(f"{email.subject} {email.body[:2000]}")
    if hint:
        word = hint.group(1).lower()
        return EmailClass(
            status={"interview": "interview", "assessment": "assessment", "offer": "offer"}[word],
            confidence=0.4,
        )
    return EmailClass(status="other", confidence=0.3)
