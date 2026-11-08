import json
from datetime import UTC, datetime, timedelta

import httpx
import mongomock
import pytest
from pymongo.errors import ServerSelectionTimeoutError

from agent import db, killswitch
from agent.cloud import Cloud
from agent.config import MongoSettings, WhatsAppSettings
from agent.notify.whatsapp import WINDOW_CLOSED_FLAG, WhatsApp

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SECRETS = {
    "whatsapp_token": "tok",
    "whatsapp_phone_number_id": "123",
    "whatsapp_to_number": "919000000001",
}


@pytest.fixture
def mongo(monkeypatch):
    # mongomock has no collection validators or collMod; accept and ignore them. The real
    # validator is checked against Atlas by `agent doctor` (mongo:validator).
    database = mongomock.database.Database
    create, command = database.create_collection, database.command

    def create_collection(self, name, **_options):
        return create(self, name)

    def run_command(self, cmd, *args, **kwargs):
        return {"ok": 1} if cmd == "collMod" else command(self, cmd, *args, **kwargs)

    monkeypatch.setattr(database, "create_collection", create_collection)
    monkeypatch.setattr(database, "command", run_command)
    return mongomock.MongoClient(tz_aware=True)


@pytest.fixture
def cloud(mongo):
    return Cloud("mongodb://unused", MongoSettings(), client_factory=lambda *a, **k: mongo, now=lambda: NOW)


def _inbound(mongo, hours_ago):
    mongo["rohit_portfolio"]["integration_messages"].insert_one(
        {"direction": "inbound", "body": "hi", "createdAt": NOW - timedelta(hours=hours_ago)}
    )


class DownClient:
    def __getitem__(self, name):
        raise ServerSelectionTimeoutError("down")

    @property
    def admin(self):
        raise ServerSelectionTimeoutError("down")

    def close(self):
        pass


# ---------------------------------------------------------------- cloud


def test_init_collections_seeds_control(cloud, mongo):
    assert set(cloud.init_collections()) == {"heartbeat", "control", "snapshot"}
    assert cloud.init_collections()  # idempotent
    assert mongo["job_agent"]["control"].find_one({"_id": "control"})["stop"] is False
    assert cloud.read_control() is False


@pytest.mark.parametrize("hours, expected", [(23.4, True), (23.6, False)])
def test_window(cloud, mongo, hours, expected):
    _inbound(mongo, 30)
    _inbound(mongo, hours)
    assert cloud.whatsapp_window_open(23.5) is expected


def test_window_unknown_without_messages(cloud):
    assert cloud.whatsapp_window_open(23.5) is None


def test_mongo_down_is_soft():
    cloud = Cloud("mongodb://unused", MongoSettings(), client_factory=lambda *a, **k: DownClient())
    assert cloud.read_control() is None
    assert cloud.last_inbound_at() is None
    assert cloud.write_heartbeat(5, True) is False
    assert cloud.ping() is False


def test_heartbeat(cloud, mongo):
    assert cloud.write_heartbeat(60, True)
    doc = mongo["job_agent"]["heartbeat"].find_one({"_id": "worker"})
    assert set(doc) == {"_id", "sent_at", "version", "uptime_s", "last_run_ok"}


def test_agent_never_writes_bot_dbs(cloud, mongo):
    cloud.init_collections()
    cloud.write_heartbeat(1, True)
    cloud.read_control()
    cloud.last_inbound_at()
    assert mongo["rohit_portfolio"].list_collection_names() == []
    assert mongo["portfolio"].list_collection_names() == []


# ---------------------------------------------------------------- whatsapp


@pytest.fixture
def http_log():
    return []


def _wa(conn, cloud, http_log, reply=(200, {"messages": [{"id": "x"}]})):
    def handler(request):
        http_log.append(request)
        return httpx.Response(reply[0], json=reply[1])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return WhatsApp(
        WhatsAppSettings(), conn, cloud, secret=lambda name: SECRETS[str(name)], http=http, now=lambda: NOW
    )


def test_closed_window_queues_without_request(conn, cloud, mongo, http_log):
    _inbound(mongo, 30)
    assert _wa(conn, cloud, http_log).send_text("hello") == "queued"
    assert http_log == []
    row = conn.execute("SELECT status, body FROM notifications").fetchone()
    assert tuple(row) == ("queued", "hello")


def test_open_window_sends_plain_text(conn, cloud, mongo, http_log):
    _inbound(mongo, 1)
    assert _wa(conn, cloud, http_log).send_text("hello") == "sent"
    payload = json.loads(http_log[0].content)
    assert payload["type"] == "text" and "template" not in payload
    assert payload["to"] == "919000000001"


def test_no_cloud_means_queue(conn, http_log):
    assert _wa(conn, None, http_log).send_text("hello") == "queued"
    assert http_log == []


def test_mongo_down_means_queue(conn, http_log):
    down = Cloud("mongodb://unused", MongoSettings(), client_factory=lambda *a, **k: DownClient())
    assert _wa(conn, down, http_log).send_text("hello") == "queued"
    assert http_log == []


def test_131047_marks_closed_until_newer_inbound(conn, cloud, mongo, http_log):
    _inbound(mongo, 1)
    wa = _wa(conn, cloud, http_log, reply=(400, {"error": {"code": 131047}}))
    assert wa.send_text("first") == "queued"
    assert db.get_flag(conn, WINDOW_CLOSED_FLAG)
    assert wa.send_text("second") == "queued"
    assert len(http_log) == 1  # second message did not even try

    _inbound(mongo, 0)  # Rohit messages the bot: window really open again
    assert _wa(conn, cloud, http_log).send_text("third") == "sent"


def test_other_error_fails(conn, cloud, mongo, http_log):
    _inbound(mongo, 1)
    wa = _wa(conn, cloud, http_log, reply=(400, {"error": {"code": 131030}}))
    assert wa.send_text("x") == "failed"
    assert conn.execute("SELECT error_code FROM notifications").fetchone()[0] == "131030"


# ---------------------------------------------------------------- killswitch


def test_stop_file(conn, tmp_path):
    (tmp_path / "STOP").write_text("")
    assert killswitch.is_stopped(conn)


def test_apply_paused_flag(conn):
    db.set_flag(conn, "apply_paused", "1")
    assert killswitch.is_stopped(conn)


def test_remote_stop_and_cache(conn, cloud, mongo):
    cloud.init_collections()
    assert not killswitch.is_stopped(conn, cloud)
    mongo["job_agent"]["control"].update_one({"_id": "control"}, {"$set": {"stop": True}})
    assert killswitch.is_stopped(conn, cloud)
    down = Cloud("mongodb://unused", MongoSettings(), client_factory=lambda *a, **k: DownClient())
    assert killswitch.is_stopped(conn, down)  # last known value is used
