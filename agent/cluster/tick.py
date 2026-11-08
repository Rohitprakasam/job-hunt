from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from agent import db
from agent.cluster.client import InstaBoxClient
from agent.cluster.config import ClusterConfig, MachineNode, load_cluster_config

log = logging.getLogger(__name__)

# Strict allowed roles for MongoDB document (sanitizer)
ALLOWED_ROLES = frozenset(
    [
        "master",
        "fast-llm",
        "strong-llm",
        "uncensored-llm",
        "research",
        "worker",
        "reserved",
    ]
)


def sanitize_cluster_doc(raw_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strict validator: ensures ONLY role, state, since, last_ok are stored.
    NEVER leaks URLs, machine_ids, tokens, or IP addresses."""
    clean = []
    for item in raw_nodes:
        role = str(item.get("role", "")).strip().lower()
        if role not in ALLOWED_ROLES:
            continue
        clean.append(
            {
                "role": role,
                "name": str(item.get("name", "")),
                "state": str(item.get("status", "unknown")),
                "since": item.get("last_active") or db.utcnow(),
                "last_ok": bool(item.get("last_ok", True)),
            }
        )
    return clean


def tick(
    config: ClusterConfig | None = None,
    client: InstaBoxClient | None = None,
    cloud: Any | None = None,
) -> dict[str, Any]:
    cfg = config or load_cluster_config()
    ib = client or InstaBoxClient()
    now = datetime.now(UTC)

    states = []
    worker_node: MachineNode | None = None

    # 1. Read each machine's state (without waking)
    for node in cfg.machines:
        st = ib.get_machine_state(node)
        states.append(st)
        if node.role == "worker":
            worker_node = node

    # 2. Trigger worker cycle if idle / not already busy
    worker_job_id: str | None = None
    if worker_node:
        cmd = "setsid nohup agent run-once cycle >> /data/job-agent/logs/cycle.log 2>&1 < /dev/null & echo $!"
        worker_job_id = ib.exec_background(worker_node, cmd)

    # 3. Sanitize cluster status doc
    sanitized = sanitize_cluster_doc(states)

    # 4. Write sanitized cluster doc and master heartbeat to Mongo (never worker or snapshot)
    if cloud is not None:
        try:
            cloud.write_cluster({"cluster": sanitized, "generated_at": db.to_iso(now)})
            cloud.write_heartbeat(0, True, node_id="master")
        except Exception as exc:
            log.warning("Failed writing cluster doc to mongo: %s", exc)

    return {
        "timestamp": db.to_iso(now),
        "cluster": sanitized,
        "worker_dispatched": bool(worker_job_id),
        "worker_job_id": worker_job_id,
    }
