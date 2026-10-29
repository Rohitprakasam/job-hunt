"""Logging with token masking and candidate redaction applied to the final formatted text."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from agent import paths
from agent.redact import Redactor

TOKEN_PATTERNS = [
    re.compile(r"(mongodb(?:\+srv)?://)[^:/@\s]+:[^@\s]+@"),
    re.compile(r"EAA[A-Za-z0-9]{20,}"),
    re.compile(r"AQ\.[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-]+"),
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}"),
]

_HANDLER_TAG = "_job_agent_handler"


def mask_tokens(text: str) -> str:
    for pattern in TOKEN_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: f"{m.group(1)}***:***@", text)
        else:
            text = pattern.sub("***", text)
    return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, redactor: Redactor | None = None):
        super().__init__(fmt)
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        text = mask_tokens(super().format(record))
        return self.redactor.redact(text) if self.redactor else text


def setup_logging(level: str = "INFO", redactor: Redactor | None = None, console: bool = True) -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(handler)
        handler.close()
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", redactor)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            paths.logs_dir() / "agent.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
    ]
    if console:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "LiteLLM", "litellm", "pymongo"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
