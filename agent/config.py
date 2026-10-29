"""Typed, strict configuration. Unknown keys, typos and leftover TODOs fail at load time."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from agent import paths

YearMonth = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]


class ConfigError(Exception):
    """Raised with every problem in a config file listed, one per line."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- profile


class Link(Strict):
    label: str
    url: Annotated[str, StringConstraints(pattern=r"^https?://")]
    handle: str | None = None
    on_resume: bool = True


class Contact(Strict):
    full_name: str
    first_name: str
    last_name: str
    name_variants: list[str] = []
    email: Annotated[str, StringConstraints(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    phone_e164: Annotated[str, StringConstraints(pattern=r"^\+\d{10,15}$")]
    city: str | None = None
    country: str
    links: list[Link] = []


class WorkAuthorization(Strict):
    countries: list[str]
    needs_sponsorship: bool


class Graduation(Strict):
    degree: str
    institution: str
    date: YearMonth


class Salary(Strict):
    currency: str
    min: int | None = None
    target: int | None = None


class Bullet(Strict):
    id: Slug
    text: str
    skills: list[str] = []
    metrics: list[str] = []


class Experience(Strict):
    id: Slug
    company: str
    title: str
    start: YearMonth
    end: YearMonth | None = None
    location: str | None = None
    bullets: list[Bullet]


class Education(Strict):
    institution: str
    degree: str
    start: YearMonth | None = None
    end: YearMonth | None = None
    score: str | None = None


class Project(Strict):
    id: Slug
    name: str
    url: str | None = None
    tags: list[str] = []
    bullets: list[Bullet]


class Certification(Strict):
    name: str
    issuer: str | None = None
    date: YearMonth | None = None


class Profile(Strict):
    contact: Contact
    summary: str
    work_authorization: WorkAuthorization
    notice_period_days: int = Field(ge=0)
    graduation: Graduation | None = None
    salary_expectation: Salary
    experience: list[Experience]
    education: list[Education]
    skills: dict[str, list[str]]
    projects: list[Project] = []
    certifications: list[Certification] = []

    def all_bullets(self) -> Iterator[Bullet]:
        for item in [*self.experience, *self.projects]:
            yield from item.bullets

    @model_validator(mode="after")
    def _unique_ids(self) -> Profile:
        for label, ids in (
            ("bullet", [b.id for b in self.all_bullets()]),
            ("experience", [e.id for e in self.experience]),
            ("project", [p.id for p in self.projects]),
        ):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(f"duplicate {label} ids: {', '.join(dupes)}")
        return self

    def as_text(self) -> str:
        """Plain-text rendering, used to test redaction against the real profile."""
        c = self.contact
        lines = [c.full_name, *c.name_variants, c.email, c.phone_e164, *(link.url for link in c.links)]
        lines.append(self.summary)
        for e in self.experience:
            lines.append(f"{e.title} at {e.company} ({e.start} - {e.end or 'present'})")
            lines.extend(b.text for b in e.bullets)
        for p in self.projects:
            lines.append(p.name)
            lines.extend(b.text for b in p.bullets)
        return "\n".join(lines)


# ---------------------------------------------------------------- search / companies


class Search(Strict):
    roles: list[str]
    seniority: list[str] = []
    locations: list[str]
    remote_ok: bool = True
    salary_floor: int | None = None
    currency: str = "INR"
    blocklist_companies: list[str] = []
    blocklist_keywords: list[str] = []
    max_posting_age_days: int = Field(default=14, ge=1)


class Company(Strict):
    name: str
    domain: str | None = None
    ats_type: Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workable", "other"]
    ats_slug: str | None = None
    watchlist: bool = True


class Companies(Strict):
    companies: list[Company] = []


# ---------------------------------------------------------------- settings


class ModelLimit(Strict):
    rpm: int = Field(gt=0)
    rpd: int = Field(gt=0)


class LLMSettings(Strict):
    free_only: bool = True
    monthly_cap_usd: float = Field(default=0, ge=0)
    fast_models: list[str] = Field(min_length=1)
    strong_models: list[str] = Field(min_length=1)
    embedding_model: str
    limits: dict[str, ModelLimit]
    quota_headroom: float = Field(default=0.9, gt=0, le=1)
    request_timeout_s: int = Field(default=60, gt=0)
    max_retries: int = Field(default=2, ge=0)

    @model_validator(mode="after")
    def _check(self) -> LLMSettings:
        if self.monthly_cap_usd == 0 and not self.free_only:
            raise ValueError("monthly_cap_usd is 0, so free_only must be true")
        missing = [m for m in [*self.fast_models, *self.strong_models] if m not in self.limits]
        if missing:
            raise ValueError(f"no limits entry for: {', '.join(missing)}")
        return self

    def ladder(self, tier: Literal["fast", "strong"]) -> list[str]:
        return list(self.fast_models if tier == "fast" else self.strong_models)


class WhatsAppSettings(Strict):
    graph_api_version: str = "v25.0"
    window_hours: float = Field(default=23.5, gt=0, lt=24)


class MongoSettings(Strict):
    agent_db: str = "job_agent"
    bot_db: str = "rohit_portfolio"
    portfolio_db: str = "portfolio"


class RedactionSettings(Strict):
    redact_other_urls: bool = False
    min_name_token_len: int = Field(default=3, ge=1)


class Settings(Strict):
    llm: LLMSettings
    whatsapp: WhatsAppSettings = WhatsAppSettings()
    mongo: MongoSettings = MongoSettings()
    redaction: RedactionSettings = RedactionSettings()


# ---------------------------------------------------------------- loading


def _find_todos(node: Any, where: str = "") -> list[str]:
    if isinstance(node, str):
        return [where or "<root>"] if node.strip().upper().startswith("TODO") else []
    if isinstance(node, dict):
        return [hit for k, v in node.items() for hit in _find_todos(v, f"{where}.{k}" if where else str(k))]
    if isinstance(node, list):
        return [hit for i, v in enumerate(node) for hit in _find_todos(v, f"{where}[{i}]")]
    return []


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"{path.name}: file not found at {path} (copy {path.stem}.example.yaml)")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc


def load_model[M: BaseModel](model: type[M], path: Path) -> M:
    raw = _read_yaml(path)
    todos = _find_todos(raw)
    if todos:
        raise ConfigError(f"{path.name}: fill in the TODO values:\n" + "\n".join(f"  - {t}" for t in todos))
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        problems = [f"  - {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(f"{path.name}: {len(problems)} problem(s):\n" + "\n".join(problems)) from exc


def load_profile(path: Path | None = None) -> Profile:
    return load_model(Profile, path or paths.config_dir() / "profile.yaml")


def load_search(path: Path | None = None) -> Search:
    return load_model(Search, path or paths.config_dir() / "search.yaml")


def load_companies(path: Path | None = None) -> Companies:
    return load_model(Companies, path or paths.config_dir() / "companies.yaml")


def load_settings(path: Path | None = None) -> Settings:
    return load_model(Settings, path or paths.config_dir() / "settings.yaml")
