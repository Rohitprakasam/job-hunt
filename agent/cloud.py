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
            "_id": {"enum": ["worker", "master"]},
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


def _bson_schema(spec: Any) -> dict[str, Any]:
    """Mongo $jsonSchema for the sanitizer's allow-list (see agent/report/sanitize.py)."""
    if isinstance(spec, dict):
        return {
            "bsonType": "object",
            "required": list(spec),
            "additionalProperties": False,
            "properties": {k: _bson_schema(v) for k, v in spec.items()},
        }
    if isinstance(spec, list):
        return {"bsonType": "array", "maxItems": 16, "items": _bson_schema(spec[0])}
    if isinstance(spec, tuple):
        return {"enum": list(spec)}
    if spec is bool:
        return {"bsonType": "bool"}
    if spec is int:
        return {"bsonType": ["int", "long"], "minimum": 0}
    return {"bsonType": "string", "maxLength": 32}


def snapshot_validator() -> dict[str, Any]:
    from agent.report.sanitize import ALLOWED

    schema = _bson_schema(ALLOWED)
    schema["required"] = ["_id", *schema["required"]]
    schema["properties"]["_id"] = {"bsonType": "string", "pattern": r"^(latest|\d{4}-\d{2}-\d{2})$"}
    return {"$jsonSchema": schema}


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
        for name, validator in {**VALIDATORS, "snapshot": snapshot_validator()}.items():
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

    def write_heartbeat(self, uptime_s: int, last_run_ok: bool, node_id: str = "worker") -> bool:
        doc = {
            "sent_at": self._now(),
            "version": __version__,
            "uptime_s": int(uptime_s),
            "last_run_ok": last_run_ok,
        }
        result = self._soft(
            "heartbeat", lambda: self.agent_db["heartbeat"].replace_one({"_id": node_id}, doc, upsert=True)
        )
        return result is not None

    def write_cluster(self, payload: dict[str, Any]) -> bool:
        """Writes sanitized cluster status to job_agent.cluster without touching snapshot or worker heartbeat."""
        doc = {"_id": "latest", **payload}
        result = self._soft(
            "cluster", lambda: self.agent_db["cluster"].replace_one({"_id": "latest"}, doc, upsert=True)
        )
        return result is not None

    def write_snapshot(self, payload: dict[str, Any], keep_days: int = 90) -> bool:
        """Latest snapshot plus one per day (90-day history). Payload must already be sanitized."""
        from agent.report.sanitize import validate

        validate(payload)
        day = payload["sent_at"][:10]
        snapshots = self.agent_db["snapshot"]

        def write() -> bool:
            snapshots.replace_one({"_id": "latest"}, {"_id": "latest", **payload}, upsert=True)
            snapshots.replace_one({"_id": day}, {"_id": day, **payload}, upsert=True)
            cutoff = (self._now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
            snapshots.delete_many({"_id": {"$lt": cutoff, "$ne": "latest"}})
            return True

        return bool(self._soft("snapshot", write))

    def set_control(self, stop: bool, set_by: str = "cli") -> bool:
        result = self._soft(
            "set_control",
            lambda: self.agent_db["control"].replace_one(
                {"_id": "control"}, {"stop": stop, "set_at": self._now(), "set_by": set_by}, upsert=True
            ),
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

    # ------------------------------------------------------------ cluster machines & metrics

    def write_machine_metrics(self, node_name: str, metrics: dict[str, Any]) -> bool:
        doc = {
            "_id": node_name,
            "name": node_name,
            "updated_at": self._now(),
            **metrics,
        }
        result = self._soft(
            "machine_metrics",
            lambda: self.agent_db["machines"].replace_one({"_id": node_name}, doc, upsert=True),
        )
        return result is not None

    def read_machine_metrics(self, node_name: str | None = None) -> list[dict[str, Any]]:
        def query() -> list[dict[str, Any]]:
            if node_name:
                doc = self.agent_db["machines"].find_one({"_id": node_name})
                return [doc] if doc else []
            return list(self.agent_db["machines"].find({}))

        res = self._soft("read_machine_metrics", query)
        return res if res is not None else []

    def read_inbox(self) -> list[dict[str, Any]]:
        """Read pending user commands from job_agent.inbox."""

        def query() -> list[dict[str, Any]]:
            return list(self.agent_db["inbox"].find({}).sort("at", 1))

        res = self._soft("read_inbox", query)
        return res if res is not None else []

    def delete_inbox_item(self, item_id: Any) -> bool:
        """Delete processed inbox command."""
        result = self._soft(
            "delete_inbox_item",
            lambda: self.agent_db["inbox"].delete_one({"_id": item_id}),
        )
        return result is not None
