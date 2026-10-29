"""MongoDB Atlas bridge (shared with rohit-s-modern-profile).

The agent writes only to the `job_agent` database, which holds no personal data, and reads the
RohitOS bot's inbound-message log to know whether the free 24-hour WhatsApp window is open.
Every call fails soft: on any error it logs a code and returns None, so the worker keeps running.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import MongoClient
from pymongo.errors import CollectionInvalid, PyMongoError

from agent import __version__
from agent.config import MongoSettings

log = logging.getLogger(__name__)

HEARTBEAT_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "sent_at", "version", "uptime_s", "last_run_ok"],
        "additionalProperties": False,
        "properties": {
            "_id": {"enum": ["worker"]},
            "sent_at": {"bsonType": "date"},
            "version": {"bsonType": "string", "maxLength": 32},
            "uptime_s": {"bsonType": ["int", "long"], "minimum": 0},
            "last_run_ok": {"bsonType": "bool"},
        },
    }
}

CONTROL_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "stop", "set_at", "set_by"],
        "additionalProperties": False,
        "properties": {
            "_id": {"enum": ["control"]},
            "stop": {"bsonType": "bool"},
            "set_at": {"bsonType": "date"},
            "set_by": {"enum": ["whatsapp", "dashboard", "cli"]},
        },
    }
}

VALIDATORS = {"heartbeat": HEARTBEAT_VALIDATOR, "control": CONTROL_VALIDATOR}


class Cloud:
    def __init__(
        self,
        uri: str,
        settings: MongoSettings,
        client_factory: Callable[..., Any] = MongoClient,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.settings = settings
        self._uri = uri
        self._factory = client_factory
        self._client: Any = None
        self._now = now

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._factory(
                self._uri, serverSelectionTimeoutMS=8000, appname="job-agent", tz_aware=True
            )
        return self._client

    @property
    def agent_db(self) -> Any:
        return self.client[self.settings.agent_db]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _soft(self, what: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except PyMongoError as exc:
            log.error("mongo %s failed: %s", what, type(exc).__name__)
            return None

    # ------------------------------------------------------------ setup

    def init_collections(self) -> list[str]:
        """Create (or update) the validated collections and seed the control doc. Idempotent."""
        db = self.agent_db
        done = []
        for name, validator in VALIDATORS.items():
            try:
                db.create_collection(name, validator=validator, validationAction="error")
            except CollectionInvalid:
                db.command("collMod", name, validator=validator, validationAction="error")
            done.append(name)
        db["control"].update_one(
            {"_id": "control"},
            {"$setOnInsert": {"stop": False, "set_at": self._now(), "set_by": "cli"}},
            upsert=True,
        )
        return done

    def ping(self) -> bool:
        return bool(self._soft("ping", lambda: self.client.admin.command("ping")))

    # ------------------------------------------------------------ agent data

    def write_heartbeat(self, uptime_s: int, last_run_ok: bool) -> bool:
        doc = {
            "sent_at": self._now(),
            "version": __version__,
            "uptime_s": int(uptime_s),
            "last_run_ok": last_run_ok,
        }
        result = self._soft(
            "heartbeat", lambda: self.agent_db["heartbeat"].replace_one({"_id": "worker"}, doc, upsert=True)
        )
        return result is not None

    def read_control(self) -> bool | None:
        """True if a stop was requested, False if not, None if Mongo could not be read."""
        doc = self._soft("read_control", lambda: self.agent_db["control"].find_one({"_id": "control"}))
        if doc is None:
            return None
        return bool(doc.get("stop", False))

    # ------------------------------------------------------------ bot data (read-only)

    def last_inbound_at(self) -> datetime | None:
        def query() -> datetime | None:
            doc = self.client[self.settings.bot_db]["integration_messages"].find_one(
                {"direction": "inbound"}, sort=[("createdAt", -1)], projection={"createdAt": 1}
            )
            if not doc or not doc.get("createdAt"):
                return None
            created = doc["createdAt"]
            return created if created.tzinfo else created.replace(tzinfo=UTC)

        return self._soft("last_inbound_at", query)

    def whatsapp_window_open(self, window_hours: float) -> bool | None:
        """None when unknown (Mongo unreachable or no inbound message yet)."""
        last = self.last_inbound_at()
        if last is None:
            return None
        return self._now() - last < timedelta(hours=window_hours)

    def read_portfolio(self) -> dict[str, Any] | None:
        """Raw portfolio data in the same shape as src/data/portfolio.ts (used by profile import)."""

        def query() -> dict[str, Any]:
            pdb = self.client[self.settings.portfolio_db]
            profile = pdb["profile"].find_one({}, projection={"_id": 0}) or {}
            data: dict[str, Any] = dict(profile)
            for name in ("experience", "education", "skills", "projects", "certifications"):
                data[name] = list(pdb[name].find({}, projection={"_id": 0}))
            return data

        return self._soft("read_portfolio", query)
