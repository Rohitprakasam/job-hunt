"""Replace the candidate's contact details with placeholders before text leaves the PC.

Placeholders are deterministic (derived from the profile), so `unredact` needs no per-call mapping.
`assert_clean` is the last check before any LLM call: if a candidate value survives, nothing is sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent.config import Profile, RedactionSettings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A run of digits with the separators people use in phone numbers.
PHONE_RE = re.compile(r"(?<![\w+])\+?\d[\d\s().-]{8,}\d(?!\w)")
DATE_LIKE_RE = re.compile(r"(19|20)\d{2}[-/.](0[1-9]|1[0-2])\b")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
PLACEHOLDER_RE = re.compile(r"\[\[(?:CANDIDATE|OTHER)_[A-Z_]+?(?:_\d+)?\]\]")


class RedactionLeak(Exception):
    """A candidate value is still present. The message lists kinds only, never values."""


def _url_forms(url: str) -> list[str]:
    bare = re.sub(r"^https?://", "", url).rstrip("/")
    no_www = bare.removeprefix("www.")
    forms = {url, url.rstrip("/"), f"https://{bare}", f"http://{bare}", f"https://{no_www}", bare, no_www}
    return sorted(forms, key=len, reverse=True)


@dataclass
class Redactor:
    # (literal, placeholder, kind), matched case-insensitively at word boundaries, longest first.
    literals: list[tuple[str, str, str]]
    restore: dict[str, str]
    email: str
    phone_last10: str
    redact_other_urls: bool = False
    _patterns: list[tuple[re.Pattern[str], str, str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        ordered = sorted(self.literals, key=lambda item: len(item[0]), reverse=True)
        self._patterns = [
            (re.compile(rf"(?<![\w]){re.escape(value)}(?![\w])", re.IGNORECASE), placeholder, kind)
            for value, placeholder, kind in ordered
        ]

    @classmethod
    def from_profile(cls, profile: Profile, settings: RedactionSettings | None = None) -> Redactor:
        settings = settings or RedactionSettings()
        c = profile.contact
        min_len = settings.min_name_token_len
        literals: list[tuple[str, str, str]] = []
        restore: dict[str, str] = {}

        def add(value: str | None, placeholder: str, kind: str, original: str | None = None) -> None:
            if value and len(value.strip()) >= min_len:
                literals.append((value.strip(), placeholder, kind))
                restore.setdefault(placeholder, original if original is not None else value.strip())

        add(c.full_name, "[[CANDIDATE_NAME]]", "name")
        for variant in c.name_variants:
            add(variant, "[[CANDIDATE_NAME]]", "name", c.full_name)
        add(c.first_name, "[[CANDIDATE_FIRST_NAME]]", "name")
        add(c.last_name, "[[CANDIDATE_LAST_NAME]]", "name")
        for i, link in enumerate(c.links, start=1):
            for form in _url_forms(link.url):
                add(form, f"[[CANDIDATE_LINK_{i}]]", "link", link.url)
            add(link.handle, f"[[CANDIDATE_HANDLE_{i}]]", "handle")
        restore["[[CANDIDATE_EMAIL]]"] = c.email
        restore["[[CANDIDATE_PHONE]]"] = c.phone_e164
        return cls(
            literals=literals,
            restore=restore,
            email=c.email.lower(),
            phone_last10=re.sub(r"\D", "", c.phone_e164)[-10:],
            redact_other_urls=settings.redact_other_urls,
        )

    # ------------------------------------------------------------ transform

    def redact(self, text: str) -> str:
        if not text:
            return text
        text = EMAIL_RE.sub(
            lambda m: "[[CANDIDATE_EMAIL]]" if m.group(0).lower() == self.email else "[[OTHER_EMAIL]]", text
        )
        for pattern, placeholder, _ in self._patterns:
            text = pattern.sub(placeholder, text)
        text = PHONE_RE.sub(self._phone_sub, text)
        if self.redact_other_urls:
            text = URL_RE.sub("[[OTHER_URL]]", text)
        return text

    def _phone_sub(self, match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) < 10:
            return match.group(0)
        if digits[-10:] == self.phone_last10:
            return "[[CANDIDATE_PHONE]]"
        # Dates and timestamps ("2026-10-29 09:33", "2024-03 - 2024-04") are not phone numbers.
        if DATE_LIKE_RE.match(match.group(0)):
            return match.group(0)
        return "[[OTHER_PHONE]]"

    def unredact(self, text: str) -> str:
        """Restore candidate placeholders only. OTHER_* stays redacted."""
        if not text:
            return text
        return PLACEHOLDER_RE.sub(lambda m: self.restore.get(m.group(0), m.group(0)), text)

    # ------------------------------------------------------------ guard

    def find_leaks(self, text: str) -> list[str]:
        kinds: set[str] = set()
        if self.email in text.lower():
            kinds.add("email")
        for pattern, _, kind in self._patterns:
            if pattern.search(text):
                kinds.add(kind)
        for match in re.finditer(r"\d[\d\s().+-]*\d", text):
            digits = re.sub(r"\D", "", match.group(0))
            if len(digits) >= 10 and self.phone_last10 in digits:
                kinds.add("phone")
        return sorted(kinds)

    def assert_clean(self, text: str) -> None:
        leaks = self.find_leaks(text)
        if leaks:
            raise RedactionLeak(f"candidate data still present: {', '.join(leaks)}")
