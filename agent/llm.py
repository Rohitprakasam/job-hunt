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
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ValidationError

# Use the bundled price map; otherwise litellm fetches it from GitHub on import.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import litellm  # noqa: E402

from agent import db, quota  # noqa: E402
from agent.config import Profile, Settings  # noqa: E402
from agent.redact import RedactionLeak, Redactor  # noqa: E402
from agent.secrets import Secret, get, get_optional  # noqa: E402

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
        self._skipped_endpoints: set[str] = set()

    def is_endpoint_skipped(self, endpoint: str) -> bool:
        """Check if an endpoint is in-memory skipped or flagged in db as llm_skip:<endpoint>."""
        if endpoint in self._skipped_endpoints:
            return True
        if self.conn:
            flag = db.get_flag(self.conn, f"llm_skip:{endpoint}")
            if flag:
                try:
                    if db.parse_iso(flag) > self._now():
                        return True
                    db.clear_flag(self.conn, f"llm_skip:{endpoint}")
                except Exception:
                    return True
        return False

    def skip_endpoint(self, endpoint: str, duration_minutes: int = 60) -> None:
        """Skip an endpoint for duration_minutes in-memory and persistently in db."""
        self._skipped_endpoints.add(endpoint)
        if self.conn:
            until = self._now() + timedelta(minutes=duration_minutes)
            db.set_flag(self.conn, f"llm_skip:{endpoint}", db.to_iso(until))

    def _route(self, model: str) -> tuple[str, str | None, str | None, int]:
        if model.startswith("groq/"):
            token = get_optional(Secret.GROQ_API_KEY)
            if not token:
                raise LLMError("GROQ_API_KEY not configured")
            return model, None, token, self.llm.request_timeout_s
        if model.startswith("openrouter/"):
            token = get_optional(Secret.OPENROUTER_API_KEY)
            if not token:
                raise LLMError("OPENROUTER_API_KEY not configured")
            return model, None, token, self.llm.request_timeout_s
        if model.startswith("cloudflare/"):
            token = get_optional(Secret.CLOUDFLARE_API_TOKEN)
            account_id = get_optional(Secret.CLOUDFLARE_ACCOUNT_ID)
            if not token or not account_id:
                raise LLMError("CLOUDFLARE_API_TOKEN or CLOUDFLARE_ACCOUNT_ID not configured")
            api_base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run"
            return model, api_base, token, self.llm.request_timeout_s
        if model.startswith("mistral/"):
            token = get_optional(Secret.MISTRAL_API_KEY)
            if not token:
                raise LLMError("MISTRAL_API_KEY not configured")
            return model, None, token, self.llm.request_timeout_s
        if "/" in model:
            ep_name, tag = model.split("/", 1)
            ep = self.llm.local_endpoints.get(ep_name)
            if not ep:
                raise LLMError(f"unknown local endpoint '{ep_name}'")
            token = get_optional(ep.token_secret)
            return f"openai/{tag}", ep.api_base, token, ep.timeout_s
        return f"gemini/{model}", None, self._api_key(), self.llm.request_timeout_s

    def prewarm(self, stage: str | None = None, timeout_total_s: float = 120.0) -> dict[str, bool]:
        """Prewarm local endpoints so models are loaded in memory and sleeping microVMs wake."""
        import requests

        target_endpoints: set[str] = set()
        if stage == "score":
            if "/" in self.llm.embedding_model:
                target_endpoints.add(self.llm.embedding_model.split("/", 1)[0])
            if self.llm.fast_models and "/" in self.llm.fast_models[0]:
                target_endpoints.add(self.llm.fast_models[0].split("/", 1)[0])
        elif stage == "prepare":
            if self.llm.strong_models and "/" in self.llm.strong_models[0]:
                target_endpoints.add(self.llm.strong_models[0].split("/", 1)[0])
        else:
            target_endpoints = set(self.llm.local_endpoints.keys())

        if not target_endpoints:
            return {}

        status = {}
        deadline = time.monotonic() + timeout_total_s
        for name in target_endpoints:
            if name not in self.llm.local_endpoints:
                continue
            ep = self.llm.local_endpoints[name]
            if self.is_endpoint_skipped(name):
                log.info("endpoint %s is marked as skipped; skipping prewarm", name)
                status[name] = False
                continue

            token = get_optional(ep.token_secret)
            if not token:
                log.warning("no secret token found for endpoint %s (%s)", name, ep.token_secret)
                self.skip_endpoint(name, 60)
                status[name] = False
                continue

            base = ep.api_base.rstrip("/")
            if base.endswith("/v1"):
                models_url = f"{base}/models"
            else:
                models_url = f"{base}/v1/models"

            headers = {"Authorization": f"Bearer {token}"}
            ready = False
            while time.monotonic() < deadline:
                remaining = max(1.0, min(15.0, deadline - time.monotonic()))
                try:
                    res = requests.get(models_url, headers=headers, timeout=remaining)
                    if res.status_code == 200:
                        ready = True
                        break
                    elif res.status_code == 503:
                        retry_after = int(res.headers.get("Retry-After", "5"))
                        self._sleep(min(retry_after, 10))
                    elif res.status_code in (401, 403):
                        log.warning(
                            "endpoint %s returned HTTP %s on /models (auth failure)", name, res.status_code
                        )
                        self.skip_endpoint(name, 60)
                        ready = False
                        break
                    else:
                        log.warning("endpoint %s returned HTTP %s on /models", name, res.status_code)
                        break
                except Exception as exc:
                    log.warning("prewarm GET %s failed: %s", models_url, exc)
                    self._sleep(3)

            if not ready:
                log.warning("endpoint %s failed prewarm, skipping for 60m", name)
                self.skip_endpoint(name, 60)
                status[name] = False
                continue

            models_to_warm = [
                m for m in [*self.llm.fast_models, *self.llm.strong_models] if m.startswith(f"{name}/")
            ]
            if models_to_warm and time.monotonic() < deadline:
                warm_model = models_to_warm[0].split("/", 1)[1]
                remaining_warm = max(1.0, min(30.0, deadline - time.monotonic()))
                try:
                    litellm.completion(
                        model=f"openai/{warm_model}",
                        api_base=ep.api_base,
                        api_key=token,
                        messages=[{"role": "user", "content": "hi"}],
                        max_tokens=1,
                        timeout=remaining_warm,
                    )
                    status[name] = True
                except Exception as exc:
                    log.warning("warmup completion on %s (%s) failed: %s", name, warm_model, exc)
                    self.skip_endpoint(name, 60)
                    status[name] = False
            else:
                status[name] = True
        return status

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
            if "/" in model:
                ep_name = model.split("/", 1)[0]
                if self.is_endpoint_skipped(ep_name):
                    continue
            if model.startswith("groq/") and not get_optional(Secret.GROQ_API_KEY):
                continue
            if model.startswith("openrouter/") and not get_optional(Secret.OPENROUTER_API_KEY):
                continue
            if model.startswith("cloudflare/") and (
                not get_optional(Secret.CLOUDFLARE_API_TOKEN)
                or not get_optional(Secret.CLOUDFLARE_ACCOUNT_ID)
            ):
                continue
            if model.startswith("mistral/") and not get_optional(Secret.MISTRAL_API_KEY):
                continue
            if model in self.llm.limits:
                try:
                    quota.check(
                        self.conn, model, self.llm.limits[model], self.llm.quota_headroom, self._now()
                    )
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
        active = [
            m
            for m in ladder
            if not (
                (m.startswith("groq/") and not get_optional(Secret.GROQ_API_KEY))
                or (m.startswith("openrouter/") and not get_optional(Secret.OPENROUTER_API_KEY))
                or (
                    m.startswith("cloudflare/")
                    and (
                        not get_optional(Secret.CLOUDFLARE_API_TOKEN)
                        or not get_optional(Secret.CLOUDFLARE_ACCOUNT_ID)
                    )
                )
                or (m.startswith("mistral/") and not get_optional(Secret.MISTRAL_API_KEY))
            )
        ]
        until = min(quota.exhausted_until(self.conn, m, self._now()) or self._now() for m in active)
        db.set_flag(self.conn, "llm_paused_until", db.to_iso(until))
        raise QuotaExhausted(until)

    def embed(self, stage: str, texts: list[str], run_id: int | None = None) -> list[list[float]]:
        """Embed texts (redacted first). Raises LLMBusy/QuotaExhausted/LLMError; callers fall back."""
        model = self.llm.embedding_model
        now = self._now()
        paused = db.get_flag(self.conn, "llm_paused_until")
        if paused and db.parse_iso(paused) > now:
            raise LLMPaused(f"LLM paused until {paused}")
        if "/" in model:
            ep_name = model.split("/", 1)[0]
            if self.is_endpoint_skipped(ep_name):
                raise LLMError(f"embedding endpoint '{ep_name}' is currently skipped")
        outgoing = [self.redactor.redact(t) for t in texts]
        try:
            self.redactor.assert_clean(json.dumps(outgoing, ensure_ascii=False))
        except RedactionLeak:
            self._log(stage, "embed", None, sent=False, ok=False, error="redaction_leak", run_id=run_id)
            raise
        vectors: list[list[float]] = []
        size = self.llm.embedding_batch
        routed_model, api_base, api_key, timeout_s = self._route(model)
        for start in range(0, len(outgoing), size):
            batch = outgoing[start : start + size]
            # Google counts every text in a batch as one request, so the whole batch must fit.
            if quota.exhausted_until(self.conn, model, self._now()):
                raise QuotaExhausted(quota.next_pacific_midnight(self._now()))
            model_limits = self.llm.limits.get(model, self.llm.embedding_limit)
            minute, day = quota.room(self.conn, model, model_limits, self.llm.quota_headroom, self._now())
            if day < len(batch):
                quota.mark_exhausted(self.conn, model, self._now())
                raise QuotaExhausted(quota.next_pacific_midnight(self._now()))
            if minute < len(batch):
                raise LLMBusy("embedding model at its per-minute limit")
            try:
                emb_kwargs = {
                    "model": routed_model,
                    "input": batch,
                    "timeout": timeout_s,
                }
                if api_key:
                    emb_kwargs["api_key"] = api_key
                if api_base:
                    emb_kwargs["api_base"] = api_base
                response = litellm.embedding(**emb_kwargs)
            except litellm.RateLimitError as exc:
                daily = _is_daily_limit(exc)
                if daily:
                    quota.mark_exhausted(self.conn, model, self._now())
                code = "rate_limited_day" if daily else "rate_limited_minute"
                self._log(stage, "embed", model, sent=False, ok=False, error=code, run_id=run_id)
                if daily:
                    raise QuotaExhausted(quota.next_pacific_midnight(self._now())) from exc
                raise LLMBusy("embedding rate limited") from exc
            except litellm.AuthenticationError as exc:
                self._log(stage, "embed", model, sent=False, ok=False, error="auth", run_id=run_id)
                if "/" in model:
                    ep_name = model.split("/", 1)[0]
                    self.skip_endpoint(ep_name, 60)
                raise LLMError("LLM provider rejected the API key or token") from exc
            except Exception as exc:  # any provider error: caller falls back to local similarity
                self._log(stage, "embed", model, sent=True, ok=False, error=type(exc).__name__, run_id=run_id)
                raise LLMError(f"embedding failed: {type(exc).__name__}") from exc
            for _ in batch:  # one row per text, matching how the free tier counts them
                self._log(stage, "embed", model, sent=True, ok=True, error=None, run_id=run_id)
            vectors.extend(list(item["embedding"]) for item in response.data)
        return vectors

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
        try:
            routed_model, api_base, api_key, timeout_s = self._route(model)
        except LLMError as exc:
            log.info("skipping model %s: %s", model, exc)
            self._log(stage, tier, model, sent=False, ok=False, error="not_configured", run_id=run_id)
            return None
        for attempt in range(self.llm.max_retries + 1):
            try:
                comp_kwargs = {
                    "model": routed_model,
                    "messages": outgoing,
                    "max_tokens": max_tokens,
                    "timeout": timeout_s,
                    "num_retries": 0,
                }
                if api_key:
                    comp_kwargs["api_key"] = api_key
                if api_base:
                    comp_kwargs["api_base"] = api_base
                if json_schema:
                    comp_kwargs["response_format"] = json_schema
                response = litellm.completion(**comp_kwargs)
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
            except (
                litellm.AuthenticationError,
                getattr(litellm, "PermissionDeniedError", litellm.AuthenticationError),
            ) as exc:
                self._log(stage, tier, model, sent=False, ok=False, error="auth", run_id=run_id)
                if "/" in model:
                    ep_name = model.split("/", 1)[0]
                    log.warning("endpoint %s auth failed, skipping endpoint for 60m", ep_name)
                    self.skip_endpoint(ep_name, 60)
                    return None
                raise LLMError("Gemini rejected the API key") from exc
            except (litellm.NotFoundError, litellm.BadRequestError) as exc:
                self._log(stage, tier, model, sent=False, ok=False, error=type(exc).__name__, run_id=run_id)
                return None
            except (
                litellm.APIConnectionError,
                litellm.Timeout,
                litellm.InternalServerError,
                litellm.ServiceUnavailableError,
                getattr(litellm, "BadGatewayError", Exception),
                getattr(litellm, "APIStatusError", Exception),
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

            finish_reason = getattr(response.choices[0], "finish_reason", None)
            if finish_reason == "length":
                self._log(
                    stage,
                    tier,
                    model,
                    sent=True,
                    ok=False,
                    error="truncated",
                    run_id=run_id,
                    tokens=(prompt_tokens, completion_tokens),
                    cost=cost,
                )
                return None

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
        tier: str,
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
