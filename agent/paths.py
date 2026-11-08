"""Filesystem locations. Runtime data lives outside the repo so it is never committed."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud")


def agent_home() -> Path:
    """%LOCALAPPDATA%\\job-agent unless JOB_AGENT_HOME is set. Creates data/, logs/, backups/."""
    override = os.environ.get("JOB_AGENT_HOME")
    if override:
        base = Path(override)
    else:
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
        base = Path(local) / "job-agent"
    for sub in ("data", "logs", "backups"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return agent_home() / "data" / "agent.db"


def logs_dir() -> Path:
    return agent_home() / "logs"


def backups_dir() -> Path:
    return agent_home() / "backups"


def output_dir() -> Path:
    """Resumes, screenshots, reports and prep packs (personal: outside the repo)."""
    path = agent_home() / "output"
    path.mkdir(parents=True, exist_ok=True)
    return path


def application_dir(app_id: int) -> Path:
    path = output_dir() / "applications" / f"{app_id:05d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def briefs_dir() -> Path:
    """Research briefs imported from research agent."""
    path = agent_home() / "briefs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def browser_profile_dir() -> Path:
    """Separate Chromium profile used only for applying."""
    return agent_home() / "browser-profile"


def config_dir() -> Path:
    return Path(os.environ.get("JOB_AGENT_CONFIG", REPO_ROOT / "config"))


def stop_file() -> Path:
    return Path(os.environ.get("JOB_AGENT_STOP_FILE", REPO_ROOT / "STOP"))


def is_cloud_synced(path: Path) -> bool:
    lowered = str(path).lower()
    return any(marker in lowered for marker in _SYNC_MARKERS)
