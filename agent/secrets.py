"""Secrets in the OS keychain (Windows Credential Manager). Values are never printed or logged."""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "job-agent"
# Credential Manager stores at most 2,560 bytes as UTF-16, so keep every value short.
MAX_LEN = 1200


class Secret(StrEnum):
    GEMINI_API_KEY = "gemini_api_key"  # pragma: allowlist secret (a keychain entry name)
    WHATSAPP_TOKEN = "whatsapp_token"
    WHATSAPP_PHONE_NUMBER_ID = "whatsapp_phone_number_id"
    WHATSAPP_TO_NUMBER = "whatsapp_to_number"
    WHATSAPP_BUSINESS_ACCOUNT_ID = "whatsapp_business_account_id"
    MONGODB_URI = "mongodb_uri"
    GROQ_API_KEY = "groq_api_key"  # pragma: allowlist secret
    OPENROUTER_API_KEY = "openrouter_api_key"  # pragma: allowlist secret
    CLOUDFLARE_API_TOKEN = "cloudflare_api_token"  # pragma: allowlist secret
    CLOUDFLARE_ACCOUNT_ID = "cloudflare_account_id"
    MISTRAL_API_KEY = "mistral_api_key"  # pragma: allowlist secret


REQUIRED: tuple[Secret, ...] = (
    Secret.GEMINI_API_KEY,
    Secret.WHATSAPP_TOKEN,
    Secret.WHATSAPP_PHONE_NUMBER_ID,
    Secret.WHATSAPP_TO_NUMBER,
    Secret.WHATSAPP_BUSINESS_ACCOUNT_ID,
    Secret.MONGODB_URI,
)

# .env name -> keychain name. Anything not listed here is never imported.
ENV_IMPORT_MAP: dict[str, Secret] = {
    "GEMINI_API_KEY": Secret.GEMINI_API_KEY,
    "WHATSAPP_ACCESS_TOKEN": Secret.WHATSAPP_TOKEN,
    "WHATSAPP_PHONE_NUMBER_ID": Secret.WHATSAPP_PHONE_NUMBER_ID,
    "WHATSAPP_RECIPIENT_PHONE": Secret.WHATSAPP_TO_NUMBER,
    "WHATSAPP_BUSINESS_ACCOUNT_ID": Secret.WHATSAPP_BUSINESS_ACCOUNT_ID,
}
# Listed only so the import report can say why they were skipped.
NEVER_IMPORT = frozenset(
    {
        "MONGODB_URI",
        "GITHUB_TOKEN",
        "ADMIN_PASSWORD",
        "CRON_SECRET",
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_VERIFY_TOKEN",
    }
)


class MissingSecret(Exception):
    def __init__(self, name: str):
        super().__init__(f"secret '{name}' is not set; run: agent secrets set {name}")
        self.name = name


class SecretTooLong(ValueError):
    pass


def _from_env(name: str) -> str | None:
    raw = str(name).strip()
    clean = re.sub(r"[^A-Za-z0-9_]", "_", raw).upper()
    return os.getenv(f"JOB_AGENT_SECRET_{clean}") or None


def get(name: Secret | str) -> str:
    env_val = _from_env(str(name))
    if env_val:
        return env_val
    try:
        value = keyring.get_password(SERVICE, str(name))
    except Exception as exc:
        raise MissingSecret(str(name)) from exc
    if not value:
        raise MissingSecret(str(name))
    return value


def get_optional(name: Secret | str) -> str | None:
    env_val = _from_env(str(name))
    if env_val:
        return env_val
    try:
        return keyring.get_password(SERVICE, str(name)) or None
    except Exception:
        return None


def set_secret(name: Secret | str, value: str) -> None:
    kr = keyring.get_keyring()
    kr_name = kr.__class__.__name__.lower()
    if "plaintext" in kr_name:
        clean = re.sub(r"[^A-Za-z0-9_]", "_", str(name)).upper()
        raise RuntimeError(
            f"refusing to store secret in plaintext keyring ({kr.__class__.__name__}); "
            f"set JOB_AGENT_SECRET_{clean} in environment instead"
        )
    value = value.strip()
    if not value:
        raise ValueError("empty value")
    if len(value) > MAX_LEN:
        raise SecretTooLong(f"'{name}' is {len(value)} chars; the limit is {MAX_LEN}")
    if str(name) == Secret.WHATSAPP_TO_NUMBER:
        value = normalize_phone(value)
    keyring.set_password(SERVICE, str(name), value)


def delete(name: Secret | str) -> bool:
    try:
        keyring.delete_password(SERVICE, str(name))
        return True
    except PasswordDeleteError:
        return False


def status() -> dict[str, bool]:
    return {s.value: get_optional(s) is not None for s in REQUIRED}


def all_status() -> dict[str, bool]:
    return {s.value: get_optional(s) is not None for s in Secret}


def purge() -> list[str]:
    return [s.value for s in Secret if delete(s)]


def normalize_phone(raw: str) -> str:
    """E.164 digits without '+'. A bare 10-digit number is assumed to be Indian (+91)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "91" + digits
    if not 11 <= len(digits) <= 15:
        raise ValueError("phone number must have 10-15 digits")
    return digits


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def import_env(path: Path) -> tuple[list[str], list[str]]:
    """Copy allow-listed values into the keychain. Returns (imported names, skipped env names)."""
    env = parse_env_file(path)
    imported: list[str] = []
    for env_name, secret in ENV_IMPORT_MAP.items():
        if env.get(env_name):
            set_secret(secret, env[env_name])
            imported.append(secret.value)
    skipped = sorted(k for k in env if k not in ENV_IMPORT_MAP)
    return imported, skipped
