import json
import threading
import urllib.request
from http.server import HTTPServer

import pytest

from agent import trigger


@pytest.fixture
def test_server(tmp_path, monkeypatch):
    monkeypatch.setattr(trigger, "_last_start_file", lambda: tmp_path / "last_cycle_start.txt")
    monkeypatch.setattr(trigger, "dispatch_cycle", lambda: True)

    server = HTTPServer(("127.0.0.1", 0), trigger.TriggerHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def test_trigger_post_cycle_started(test_server):
    req = urllib.request.Request(f"{test_server}/cycle", data=b"", method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data.get("status") == "started"


def test_trigger_post_cycle_busy(test_server, monkeypatch):
    monkeypatch.setattr(trigger, "dispatch_cycle", lambda: False)
    req = urllib.request.Request(f"{test_server}/cycle", data=b"", method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data.get("status") == "busy"


def test_trigger_not_found(test_server):
    req = urllib.request.Request(f"{test_server}/not_found", data=b"", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 404


def test_trigger_get_not_found(test_server):
    req = urllib.request.Request(f"{test_server}/cycle", method="GET")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 404
