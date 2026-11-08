import logging

import pytest
from typer.testing import CliRunner

from agent import paths
from agent import secrets as sec
from agent.cli import app
from agent.logging_setup import setup_logging
from agent.redact import Redactor

# Fake values, assembled so secret scanners don't flag the fixture itself.
FAKE_URI = "mongodb+srv://" + "admin" + ":" + "pw" + "@cluster.example.net/"
ENV = f"""\
# comment
MONGODB_URI={FAKE_URI}
GITHUB_TOKEN=ghp_fakefakefakefakefakefake
GEMINI_API_KEY="{"gem" + "-key"}"
WHATSAPP_APP_SECRET=nope
WHATSAPP_ACCESS_TOKEN=wa-token
WHATSAPP_PHONE_NUMBER_ID=1038
WHATSAPP_BUSINESS_ACCOUNT_ID=1349
WHATSAPP_RECIPIENT_PHONE=9000000001
GRAPH_API_VERSION=v25.0
"""


def test_import_env_allow_list(tmp_path, isolated):
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")
    imported, skipped = sec.import_env(env)
    assert sorted(imported) == sorted(s.value for s in sec.ENV_IMPORT_MAP.values())
    assert "MONGODB_URI" in skipped and "GITHUB_TOKEN" in skipped
    stored = {user for (_, user) in isolated.store}
    assert "mongodb_uri" not in stored
    assert sec.get(sec.Secret.GEMINI_API_KEY) == "gem-key"
    assert sec.get(sec.Secret.WHATSAPP_TO_NUMBER) == "919000000001"


def test_too_long_rejected():
    with pytest.raises(sec.SecretTooLong):
        sec.set_secret(sec.Secret.WHATSAPP_TOKEN, "x" * (sec.MAX_LEN + 1))


def test_missing_secret_has_hint():
    with pytest.raises(sec.MissingSecret, match="agent secrets set gemini_api_key"):
        sec.get(sec.Secret.GEMINI_API_KEY)


def test_status_and_purge():
    sec.set_secret(sec.Secret.GEMINI_API_KEY, "k")
    assert sec.status()["gemini_api_key"] is True
    assert sec.purge() == ["gemini_api_key"]
    assert not any(sec.status().values())


def test_cli_list_never_prints_values():
    sec.set_secret(sec.Secret.WHATSAPP_TOKEN, "super-secret-value")
    result = CliRunner().invoke(app, ["secrets", "list"])
    assert result.exit_code == 0
    assert "super-secret-value" not in result.output
    assert "whatsapp_token" in result.output


def test_log_file_is_masked_and_redacted(profile):
    setup_logging(redactor=Redactor.from_profile(profile), console=False)
    logging.getLogger("t").warning(
        "mail %s key %s uri %s",
        "testa.candidato@example.com",
        "sk-abcdefghijklmnopqrstuvwxyz",  # pragma: allowlist secret
        "mongodb+srv://user:pass@host.example.net/db",  # pragma: allowlist secret
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (paths.logs_dir() / "agent.log").read_text(encoding="utf-8")
    assert "example.com" not in text.replace("host.example.net", "")
    assert "sk-abc" not in text and "user:pass" not in text
    assert "[[CANDIDATE_EMAIL]]" in text
    logging.getLogger().handlers.clear()


def test_secrets_read_from_env(monkeypatch, isolated):
    monkeypatch.setenv("JOB_AGENT_SECRET_LLM_TOKEN_ROHIT_2", "env-token-123")
    assert sec.get("llm_token_rohit_2") == "env-token-123"
    assert sec.get_optional("llm_token_rohit_2") == "env-token-123"

    # Bare env names must be ignored
    monkeypatch.setenv("MONGODB_URI", "bare_uri_should_be_ignored")
    assert sec.get_optional(sec.Secret.MONGODB_URI) is None
    monkeypatch.setenv("JOB_AGENT_SECRET_MONGODB_URI", "prefixed_uri_used")
    assert sec.get(sec.Secret.MONGODB_URI) == "prefixed_uri_used"


def test_set_secret_refuses_plaintext_keyring(monkeypatch):
    class FakePlaintextKeyring:
        pass

    monkeypatch.setattr(sec.keyring, "get_keyring", lambda: FakePlaintextKeyring())
    with pytest.raises(RuntimeError, match="refusing to store secret in plaintext keyring"):
        sec.set_secret("test_secret", "some_value")
