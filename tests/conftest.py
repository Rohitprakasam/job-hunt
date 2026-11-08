from __future__ import annotations

from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from agent import config, db

EXAMPLES = Path(__file__).resolve().parent.parent / "config"


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own agent home, stop file and in-memory keychain."""
    import os

    monkeypatch.setenv("JOB_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JOB_AGENT_STOP_FILE", str(tmp_path / "STOP"))
    for k in list(os.environ.keys()):
        if k.startswith("JOB_AGENT_SECRET_"):
            monkeypatch.delenv(k, raising=False)
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def profile() -> config.Profile:
    return config.load_profile(EXAMPLES / "profile.example.yaml")


@pytest.fixture
def settings() -> config.Settings:
    return config.load_settings(EXAMPLES / "settings.example.yaml")


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.migrate(connection)
    yield connection
    connection.close()
