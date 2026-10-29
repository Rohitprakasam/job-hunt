"""WhatsApp Cloud API at $0: plain text only, and only while the 24-hour service window is open.

There is deliberately no template-sending code here. Template messages are billed; plain text
sent outside the window is rejected by Meta (error 131047) without charge.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from agent import db
from agent.cloud import Cloud
from agent.config import WhatsAppSettings
from agent.secrets import Secret, get

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
WINDOW_CLOSED_FLAG = "wa_window_closed_until"
WINDOW_CLOSED_INBOUND_FLAG = "wa_window_closed_inbound"

ERROR_HINTS = {
    131047: "24-hour window closed: message the RohitOS bot, then the queue is sent",
    131030: "recipient not allowed: add it in Meta -> WhatsApp -> API Setup -> To (test numbers allow 5)",
    190: "access token expired or invalid: generate a System User token with no expiry",
}

SendStatus = Literal["sent", "queued", "failed"]


class WhatsApp:
    def __init__(
        self,
        settings: WhatsAppSettings,
        conn: sqlite3.Connection,
        cloud: Cloud | None,
        secret: Callable[[Secret], str] = get,
        http: httpx.Client | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.settings = settings
        self.conn = conn
        self.cloud = cloud
        self._secret = secret
        self._http = http or httpx.Client(timeout=20)
        self._now = now

    def _url(self, path: str) -> str:
        return f"{GRAPH}/{self.settings.graph_api_version}/{path}"

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret(Secret.WHATSAPP_TOKEN)}"}

    # ------------------------------------------------------------ window

    def window_open(self) -> bool:
        """Conservative: unknown counts as closed, and a recent 131047 overrides the inbound log
        until a newer inbound message arrives."""
        if self.cloud is None:
            return False
        last = self.cloud.last_inbound_at()
        if last is None:
            return False
        closed_until = db.get_flag(self.conn, WINDOW_CLOSED_FLAG)
        if closed_until and db.parse_iso(closed_until) > self._now():
            seen = db.get_flag(self.conn, WINDOW_CLOSED_INBOUND_FLAG)
            if seen is None or last <= db.parse_iso(seen):
                return False
        return self._now() - last < timedelta(hours=self.settings.window_hours)

    # ------------------------------------------------------------ send

    def send_text(
        self, body: str, kind: str = "system", priority: int = 0, ref_id: str | None = None
    ) -> SendStatus:
        if not self.window_open():
            self._record(kind, body, priority, ref_id, "queued")
            return "queued"
        payload = {
            "messaging_product": "whatsapp",
            "to": self._secret(Secret.WHATSAPP_TO_NUMBER),
            "type": "text",
            "text": {"body": body[:4096]},
        }
        try:
            response = self._http.post(
                self._url(f"{self._secret(Secret.WHATSAPP_PHONE_NUMBER_ID)}/messages"),
                headers=self._auth(),
                json=payload,
            )
        except httpx.HTTPError as exc:
            log.error("whatsapp send failed: %s", type(exc).__name__)
            self._record(kind, body, priority, ref_id, "queued", error="network")
            return "queued"
        if response.status_code == 200:
            self._record(kind, body, priority, ref_id, "sent")
            return "sent"
        code = error_code(response)
        if code == 131047:
            self._mark_window_closed()
            self._record(kind, body, priority, ref_id, "queued", error="131047")
            return "queued"
        log.error("whatsapp send rejected: code=%s hint=%s", code, ERROR_HINTS.get(code or 0, "-"))
        self._record(kind, body, priority, ref_id, "failed", error=str(code))
        return "failed"

    def _mark_window_closed(self) -> None:
        db.set_flag(self.conn, WINDOW_CLOSED_FLAG, db.to_iso(self._now() + timedelta(hours=1)))
        last = self.cloud.last_inbound_at() if self.cloud else None
        if last is not None:
            db.set_flag(self.conn, WINDOW_CLOSED_INBOUND_FLAG, db.to_iso(last))

    def _record(
        self, kind: str, body: str, priority: int, ref_id: str | None, status: str, error: str | None = None
    ) -> None:
        now = db.to_iso(self._now())
        self.conn.execute(
            "INSERT INTO notifications(channel, kind, ref_id, body, priority, status, error_code, queued_at,"
            " sent_at) VALUES ('whatsapp', ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, ref_id, body, priority, status, error, now, now if status == "sent" else None),
        )

    # ------------------------------------------------------------ checks

    def phone_number_info(self) -> dict[str, Any]:
        response = self._http.get(
            self._url(self._secret(Secret.WHATSAPP_PHONE_NUMBER_ID)),
            headers=self._auth(),
            params={"fields": "display_phone_number,verified_name,quality_rating"},
        )
        response.raise_for_status()
        return response.json()

    def token_info(self) -> dict[str, Any]:
        token = self._secret(Secret.WHATSAPP_TOKEN)
        response = self._http.get(
            self._url("debug_token"),
            params={"input_token": token},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json().get("data", {})


def error_code(response: httpx.Response) -> int | None:
    try:
        return int(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None
