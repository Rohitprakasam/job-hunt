"""Structural guarantees that must hold as the codebase grows."""

import re
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent / "agent"


def _sources():
    return {p.relative_to(AGENT).as_posix(): p.read_text(encoding="utf-8") for p in AGENT.rglob("*.py")}


def test_only_llm_module_imports_litellm():
    offenders = [
        name
        for name, src in _sources().items()
        if re.search(r"^\s*(import litellm|from litellm)", src, re.M) and name != "llm.py"
    ]
    assert offenders == []


def test_whatsapp_token_never_read_from_mongo_settings():
    # /api/settings in the portfolio app has no auth, so its stored token is untrusted.
    assert all("whatsappToken" not in src for src in _sources().values())


def test_no_whatsapp_template_messages():
    src = _sources()["notify/whatsapp.py"]
    assert '"template"' not in src and "'template'" not in src
