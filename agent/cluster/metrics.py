"""System and machine resource metrics collection for cluster nodes.

Lightweight: reads /proc/meminfo, /proc/uptime, os.getloadavg, and runs ps/docker.
Zero heavy dependencies (no psutil required).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


def read_uptime() -> int:
    """Uptime in seconds from /proc/uptime, or 0 if unavailable."""
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return 0


def read_memory() -> dict[str, int]:
    """Total, used, available memory in MB from /proc/meminfo."""
    mem: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].split()[0]) // 1024
    except Exception:
        return {"total_mb": 0, "used_mb": 0, "pct_used": 0}

    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(0, total - avail)
    pct = round((used / total) * 100, 1) if total > 0 else 0
    return {"total_mb": total, "used_mb": used, "pct_used": pct}


def read_disk(path: str | None = None) -> dict[str, Any]:
    """Disk usage for mount point in GB."""
    try:
        target_path = path or os.path.abspath(os.sep)
        usage = shutil.disk_usage(target_path)
        total = round(usage.total / (1024**3), 1)
        free = round(usage.free / (1024**3), 1)
        used = round(usage.used / (1024**3), 1)
        pct = round((usage.used / usage.total) * 100, 1) if usage.total > 0 else 0
        return {"total_gb": total, "free_gb": free, "used_gb": used, "pct_used": pct}
    except Exception:
        return {"total_gb": 0.0, "free_gb": 0.0, "used_gb": 0.0, "pct_used": 0.0}


def read_top_processes(limit: int = 6) -> list[str]:
    """Top running processes sorted by CPU usage."""
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            lines = [line.strip() for line in res.stdout.splitlines()[1:] if line.strip()]
            return lines[:limit]
    except Exception:
        pass
    return []


def read_ollama_models() -> list[str]:
    """Check if Ollama is running locally and list loaded/available models."""
    try:
        import urllib.request

        req = urllib.request.Request("http://127.0.0.1:11434/api/tags", headers={"User-Agent": "job-agent"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def collect_local_metrics(node_name: str | None = None) -> dict[str, Any]:
    """Collect full local machine resource usage and top processes."""
    name = node_name or os.getenv("INSTABOX_MACHINE_NAME") or platform.node()
    load = list(os.getloadavg()) if hasattr(os, "getloadavg") else [0.0, 0.0, 0.0]
    mem = read_memory()
    disk = read_disk()
    procs = read_top_processes(limit=6)
    uptime = read_uptime()
    models = read_ollama_models()

    return {
        "name": name,
        "state": "running",
        "uptime_s": uptime,
        "load": [round(x, 2) for x in load],
        "mem_total_mb": mem["total_mb"],
        "mem_used_mb": mem["used_mb"],
        "mem_pct": mem["pct_used"],
        "disk_total_gb": disk["total_gb"],
        "disk_free_gb": disk["free_gb"],
        "top_procs": procs,
        "models": models,
        "updated_at": datetime.now(UTC).isoformat(),
    }
