"""Everything a stage needs, built once per stage run (SQLite connections are per thread)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from agent import config, db
from agent import secrets as sec

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    settings: config.Settings
    search: config.Search
    companies: config.Companies
    profile: config.Profile | None
    conn: Any
    cloud: Any
    client: Any
    wa: Any


@contextmanager
def open_ctx(need_profile: bool = True, online: bool = True) -> Iterator[Ctx]:
    settings = config.load_settings()
    search = config.load_search()
    companies = config.load_companies()
    profile = config.load_profile() if need_profile else None
    conn = db.connect()
    db.migrate(conn)
    cloud = client = None
    if online and sec.get_optional(sec.Secret.MONGODB_URI):
        from agent.cloud import Cloud

        cloud = Cloud(sec.get(sec.Secret.MONGODB_URI), settings.mongo)
    if online and profile is not None and sec.get_optional(sec.Secret.GEMINI_API_KEY):
        from agent.llm import LLMClient

        client = LLMClient(settings, profile, conn)
    from agent.notify.whatsapp import WhatsApp

    wa = WhatsApp(settings.whatsapp, conn, cloud)
    try:
        yield Ctx(settings, search, companies, profile, conn, cloud, client, wa)
    finally:
        if cloud is not None:
            cloud.close()
        conn.close()
