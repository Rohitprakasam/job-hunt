from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from agent.cluster.config import ClusterConfig, MachineNode
from agent.cluster.tick import sanitize_cluster_doc, tick


def test_cluster_config_validation():
    node = MachineNode(
        name="rohit-1",
        role="master",
        workspace="default",
        machine_id="m_12345",
    )
    cfg = ClusterConfig(machines=[node])
    assert cfg.machines[0].name == "rohit-1"
    assert cfg.machines[0].role == "master"

    # Invalid role fails
    with pytest.raises(ValidationError):
        MachineNode(
            name="rohit-bad",
            role="invalid-role",  # type: ignore
            workspace="default",
            machine_id="m_99999",
        )


def test_sanitize_cluster_doc_strips_sensitive_data():
    raw_nodes = [
        {
            "name": "rohit-2",
            "role": "fast-llm",
            "machine_id": "m_secret_id_do_not_leak",
            "token": "ibp_secret_token_123",
            "url": "https://secret.endpoint.instabox.tech",
            "status": "ready",
            "last_active": "2026-11-07T06:00:00Z",
            "last_ok": True,
        },
        {
            "name": "rohit-malicious",
            "role": "hacker-role",  # not in ALLOWED_ROLES
            "status": "active",
        },
    ]

    cleaned = sanitize_cluster_doc(raw_nodes)
    assert len(cleaned) == 1
    doc = cleaned[0]

    assert doc["role"] == "fast-llm"
    assert doc["name"] == "rohit-2"
    assert doc["state"] == "ready"
    assert doc["since"] == "2026-11-07T06:00:00Z"
    assert doc["last_ok"] is True

    # Critical security assertion: NO IDs, tokens, or URLs leaked
    for key in ("machine_id", "token", "url", "id"):
        assert key not in doc


def test_cluster_tick_dispatches_worker():
    nodes = [
        MachineNode(name="rohit-1", role="master", machine_id="m_1"),
        MachineNode(name="rohit-6", role="worker", machine_id="m_6"),
    ]
    cfg = ClusterConfig(machines=nodes)

    mock_client = MagicMock()
    mock_client.get_machine_state.side_effect = lambda node: {
        "id": node.machine_id,
        "name": node.name,
        "role": node.role,
        "status": "ready",
        "last_active": "2026-11-07T06:30:00Z",
    }
    mock_client.exec_background.return_value = "job_12345"

    result = tick(config=cfg, client=mock_client)
    assert result["worker_dispatched"] is True
    assert result["worker_job_id"] == "job_12345"
    assert len(result["cluster"]) == 2
    mock_client.exec_background.assert_called_once()


def test_exec_background_returns_pid_and_fails_on_nonzero_or_error(monkeypatch):
    import httpx

    from agent.cluster.client import InstaBoxClient

    client = InstaBoxClient(token="test-key")
    node = MachineNode(name="rohit-6", role="worker", machine_id="m_6")

    # Documented REST exec success: HTTP 200, exitCode 0, PID in stdout
    def fake_post_ok(url, **kwargs):
        assert kwargs["json"]["timeoutSeconds"] == 30
        assert "setsid nohup" in kwargs["json"]["cmd"]
        return httpx.Response(200, json={"stdout": "99999\n", "stderr": "", "exitCode": 0})

    monkeypatch.setattr(httpx, "post", fake_post_ok)
    assert (
        client.exec_background(
            node,
            "setsid nohup agent run-once cycle >> /data/job-agent/logs/cycle.log 2>&1 < /dev/null & echo $!",
        )
        == "99999"
    )

    # Non-zero exitCode counts as failure
    def fake_post_failed_exit(url, **kwargs):
        return httpx.Response(200, json={"stdout": "", "stderr": "error", "exitCode": 1})

    monkeypatch.setattr(httpx, "post", fake_post_failed_exit)
    assert client.exec_background(node, "echo 1") is None

    # Error status returns None
    def fake_post_err(url, **kwargs):
        return httpx.Response(500, text="internal error")

    monkeypatch.setattr(httpx, "post", fake_post_err)
    assert client.exec_background(node, "echo 1") is None


def test_cluster_tick_writes_cluster_and_master_heartbeat_never_worker_or_snapshot():
    mock_client = MagicMock()
    mock_client.get_machine_state.return_value = {
        "id": "m_1",
        "name": "rohit-1",
        "role": "master",
        "status": "ready",
    }
    mock_client.exec_background.return_value = "12345"

    mock_cloud = MagicMock()
    nodes = [MachineNode(name="rohit-1", role="master", machine_id="m_1")]
    cfg = ClusterConfig(machines=nodes)

    tick(config=cfg, client=mock_client, cloud=mock_cloud)

    # Must write cluster and master heartbeat
    mock_cloud.write_cluster.assert_called_once()
    mock_cloud.write_heartbeat.assert_called_once_with(0, True, node_id="master")

    # MUST NEVER touch snapshot or worker heartbeat
    assert not hasattr(mock_cloud, "write_snapshot") or mock_cloud.write_snapshot.call_count == 0


def test_exec_background_no_ssh_fallback(monkeypatch):
    from agent.cluster.client import InstaBoxClient

    client = InstaBoxClient()
    monkeypatch.delenv("JOB_AGENT_SECRET_INSTABOX_API_KEY", raising=False)

    node = MachineNode(name="rohit-6", role="worker", machine_id="m_6", ssh_host="ibssh_test")
    # With no API token, must return None immediately without invoking SSH
    assert client.exec_background(node, "echo 1") is None
