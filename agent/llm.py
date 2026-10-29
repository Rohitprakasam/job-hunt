"""The only module that talks to an LLM. Every call is redacted, leak-checked, quota-checked and logged.

Order per call: pause flag -> redact -> leak guard -> model ladder (quota check -> request) -> log -> unredact.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ValidationError

# Use the bundled price map; otherwise litellm fetches it from GitHub on import.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import litellm  # noqa: E402

from agent import db, quota  # noqa: E402
from agent.config import Profile, Settings  # noqa: E402
from agent.redact import RedactionLeak, Redactor  # noqa: E402
from agent.secrets import Secret, get  # noqa: E402

litellm.success_callback = []
litellm.failure_callback = []
litellm.callbacks = []
if hasattr(litellm, "telemetry"):
    litellm.telemetry = False
litellm.suppress_debug_info = True

log = logging.getLogger(__name__)

Tier = Literal["fast", "strong"]

PLACEHOLDER_NOTE = (
    "Text may contain placeholders such as [[CANDIDATE_NAME]] or [[OTHER_EMAIL]]. "
    "They stand for private values. Copy them exactly as written and never guess what they hide."
)


class LLMError(Exception):
    pass


class LLMPaused(LLMError):
    pass


class LLMBusy(LLMError):
    """Every model is at its per-minute limit. Try again later."""


class QuotaExhausted(LLMError):
    def __init__(self, until: datetime):
        super().__init__(f"all models out of daily quota until {db.to_iso(until)}")
        self.until = until


@dataclass
class LLMResult:
    text: str
    model: str
    data: BaseModel | None
    prompt_tokens: int | None
    completion_tokens: int | None


def _is_daily_limit(exc: Exception) -> bool:
    text = str(exc).lower().replace(" ", "").replace("_", "")
    return "perday" in text or "requestsperday" in text


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        profile: Profile,
        conn: sqlite3.Connection,
        api_key: Callable[[], str] = lambda: get(Secret.GEMINI_API_KEY),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.llm = settings.llm
        self.conn = conn
        self.redactor = Redactor.from_profile(profile, settings.redaction)
        self._api_key = api_key
        self._now = now
        self._sleep = sleep

    # ------------------------------------------------------------ public

    def complete(
        self,
        stage: str,
        tier: Tier,
        messages: list[dict[str, str]],
        max_tokens: int,
        json_schema: type[BaseModel] | None = None,
        run_id: int | None = None,
    ) -> LLMResult:
        now = self._now()
        paused = db.get_flag(self.conn, "llm_paused_until")
        if paused and db.parse_iso(paused) > now:
            raise LLMPaused(f"LLM paused until {paused}")

        outgoing = [{"role": "system", "content": PLACEHOLDER_NOTE}]
        outgoing += [{**m, "content": self.redactor.redact(m["content"])} for m in messages]
        try:
            self.redactor.assert_clean(json.dumps(outgoing, ensure_ascii=False))
        except RedactionLeak:
            self._log(stage, tier, None, sent=False, ok=False, error="redaction_leak", run_id=run_id)
            raise

        # The ladder comes only from settings, which require free_only when the budget is $0.
        ladder = self.llm.ladder(tier)
        busy = False
        for model in ladder:
            try:
                quota.check(self.conn, model, self.llm.limits[model], self.llm.quota_headroom, self._now())
            except quota.QuotaRefused as refused:
                if refused.reason == "daily limit":
                    quota.mark_exhausted(self.conn, model, self._now())
                busy |= refused.reason == "per-minute limit"
                continue
            result = self._try_model(stage, tier, model, outgoing, max_tokens, json_schema, run_id)
            if result is not None:
                return result
            busy |= quota.exhausted_until(self.conn, model, self._now()) is None

        if busy:
            raise LLMBusy("every model is rate limited or failing right now")
        until = min(quota.exhausted_until(self.conn, m, self._now()) or self._now() for m in ladder)
        db.set_flag(self.conn, "llm_paused_until", db.to_iso(until))
        raise QuotaExhausted(until)

    # ------------------------------------------------------------ internals

    def _try_model(
        self,
        stage: str,
        tier: Tier,
        model: str,
        outgoing: list[dict[str, str]],
        max_tokens: int,
        json_schema: type[BaseModel] | None,
        run_id: int | None,
    ) -> LLMResult | None:
        """Returns a result, or None to move to the next model. Raises on errors no model can fix."""
        for attempt in range(self.llm.max_retries + 1):
            try:
                response = litellm.completion(
                    model=f"gemini/{model}",
                    api_key=self._api_key(),
                    messages=outgoing,
                    max_tokens=max_tokens,
                    timeout=self.llm.request_timeout_s,
                    num_retries=0,
                    **({"response_format": json_schema} if json_schema else {}),
                )
            except litellm.RateLimitError as exc:
                if _is_daily_limit(exc):
                    quota.mark_exhausted(self.conn, model, self._now())
                    self._log(
                        stage, tier, model, sent=False, ok=False, error="rate_limited_day", run_id=run_id
                    )
                    return None
                self._log(
                    stage, tier, model, sent=False, ok=False, error="rate_limited_minute", run_id=run_id
                )
                if attempt < self.llm.max_retries:
                    self._sleep(5 * (attempt + 1))
                    continue
                return None
            except litellm.AuthenticationError as exc:
                self._log(stage, tier, model, sent=False, ok=False, error="auth", run_id=run_id)
                raise LLMError("Gemini rejected the API key") from exc
            except (litellm.NotFoundError, litellm.BadRequestError) as exc:
                self._log(stage, tier, model, sent=False, ok=False, error=type(exc).__name__, run_id=run_id)
                return None
            except (
                litellm.APIConnectionError,
                litellm.Timeout,
                litellm.InternalServerError,
                litellm.ServiceUnavailableError,
                litellm.APIError,
            ) as exc:
                self._log(stage, tier, model, sent=True, ok=False, error=type(exc).__name__, run_id=run_id)
                return None

            text = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)
            try:
                cost = litellm.completion_cost(completion_response=response)
            except Exception:  # unknown model in the price map is fine: cost is informational
                cost = None

            data = None
            if json_schema is not None:
                try:
                    data = json_schema.model_validate_json(text)
                except ValidationError:
                    self._log(
                        stage,
                        tier,
                        model,
                        sent=True,
                        ok=False,
                        error="invalid_json",
                        run_id=run_id,
                        tokens=(prompt_tokens, completion_tokens),
                        cost=cost,
                    )
                    if attempt < self.llm.max_retries:
                        continue
                    return None

            self._log(
                stage,
                tier,
                model,
                sent=True,
                ok=True,
                error=None,
                run_id=run_id,
                tokens=(prompt_tokens, completion_tokens),
                cost=cost,
            )
            restored = self.redactor.unredact(text)
            if data is not None:
                data = json_schema.model_validate_json(self.redactor.unredact(data.model_dump_json()))
            return LLMResult(restored, model, data, prompt_tokens, completion_tokens)
        return None

    def _log(
        self,
        stage: str,
        tier: Tier,
        model: str | None,
        *,
        sent: bool,
        ok: bool,
        error: str | None,
        run_id: int | None,
        tokens: tuple[int | None, int | None] = (None, None),
        cost: float | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO llm_calls(run_id, stage, tier, model, prompt_tokens, completion_tokens, list_cost_usd,"
            " sent, ok, error_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                stage,
                tier,
                model,
                tokens[0],
                tokens[1],
                cost,
                int(sent),
                int(ok),
                error,
                db.to_iso(self._now()),
            ),
        )
        if error:
            log.warning("llm %s/%s model=%s error=%s", stage, tier, model, error)
