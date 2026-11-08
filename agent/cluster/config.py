from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from agent import paths
from agent.config import ConfigError, Strict

ClusterRole = Literal[
    "master",
    "fast-llm",
    "strong-llm",
    "uncensored-llm",
    "research",
    "worker",
    "reserved",
]


class MachineNode(Strict):
    name: str
    role: ClusterRole
    workspace: str = "default"
    machine_id: str
    ssh_host: str | None = None


class ClusterConfig(Strict):
    machines: list[MachineNode] = Field(min_length=1)


def cluster_config_path() -> Path:
    return paths.config_dir() / "cluster.yaml"


def load_cluster_config(path: Path | None = None) -> ClusterConfig:
    p = path or cluster_config_path()
    if not p.exists():
        raise ConfigError(f"cluster.yaml: file not found at {p.parent}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        return ClusterConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"cluster.yaml: invalid configuration: {exc}") from exc
