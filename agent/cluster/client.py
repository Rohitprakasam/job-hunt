from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from agent import secrets as sec
from agent.cluster.config import MachineNode

log = logging.getLogger(__name__)

API_URL = os.getenv("INSTABOX_API_URL", "https://api.instabox.sh")


class InstaBoxClient:
    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("INSTABOX_API_KEY")

    def _headers(self, workspace: str = "default") -> dict[str, str]:
        tok = (
            sec.get_optional(f"INSTABOX_KEY_{workspace.upper()}")
            or os.getenv(f"INSTABOX_KEY_{workspace.upper()}")
            or self.token
        )
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def get_machine_state(self, node: MachineNode) -> dict[str, Any]:
        """Reads machine state (GET /v1/machines/{id}). Never wakes a paused microVM."""
        headers = self._headers(node.workspace)
        if not headers:
            return {"id": node.machine_id, "name": node.name, "role": node.role, "status": "unknown"}
        try:
            resp = httpx.get(f"{API_URL}/v1/machines/{node.machine_id}", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "id": node.machine_id,
                    "name": node.name,
                    "role": node.role,
                    "status": data.get("status", "unknown"),
                    "last_active": data.get("lastActiveAt") or data.get("updatedAt"),
                }
            return {
                "id": node.machine_id,
                "name": node.name,
                "role": node.role,
                "status": f"HTTP_{resp.status_code}",
            }
        except Exception as exc:
            return {
                "id": node.machine_id,
                "name": node.name,
                "role": node.role,
                "status": "error",
                "error": str(exc),
            }

    def exec_background(self, node: MachineNode, cmd: str) -> str | None:
        """Executes a command on the machine via documented REST exec (POST /v1/machines/{id}/exec).
        Expects HTTP 200, exitCode 0, and returns the PID from stdout on success, or None on failure.
        """
        headers = self._headers(node.workspace)
        if not headers:
            log.error(
                "No API token available for workspace '%s' (node %s); cannot execute via API",
                node.workspace,
                node.name,
            )
            return None
        try:
            payload = {"cmd": cmd, "timeoutSeconds": 30}
            resp = httpx.post(
                f"{API_URL}/v1/machines/{node.machine_id}/exec",
                headers=headers,
                json=payload,
                timeout=35,
            )
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                if data.get("exitCode") == 0:
                    pid = str(data.get("stdout", "")).strip()
                    if pid:
                        return pid
                    return "ok"
                log.warning("API exec on %s returned exitCode %s: %s", node.name, data.get("exitCode"), data)
                return None
            log.error("API exec failed on %s: HTTP %s %s", node.name, resp.status_code, resp.text)
            return None
        except Exception as exc:
            log.error("API exec failed on %s: %s", node.name, exc)
            return None
