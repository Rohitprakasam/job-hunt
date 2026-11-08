import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agent import db, llm, quota
from agent.redact import RedactionLeak

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
DAILY_429 = "429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"
MINUTE_429 = "429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier"


def _response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
    )


def _rate_limit(message):
    return llm.litellm.RateLimitError(message=message, llm_provider="gemini", model="m")


class FakeProvider:
    """Stands in for litellm.completion. `plan` maps model id -> list of outcomes (text or exception)."""

    def __init__(self, plan=None, default="OK"):
        self.plan = plan or {}
        self.default = default
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs["model"].removeprefix("gemini/")
        outcomes = self.plan.get(model)
        outcome = outcomes.pop(0) if outcomes else self.default
        if isinstance(outcome, Exception):
            raise outcome
        return _response(outcome)


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(llm.litellm, "completion", fake)
    monkeypatch.setattr(llm.litellm, "completion_cost", lambda **_: 0.0)
    return fake


@pytest.fixture
def client(settings, profile, conn):
    sleeps = []
    c = llm.LLMClient(
        settings, profile, conn, api_key=lambda: "test-key", now=lambda: NOW, sleep=sleeps.append
    )
    c.sleeps = sleeps
    return c


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id")]


def test_payload_is_redacted_and_output_restored(client, provider, conn):
    provider.default = "Hello [[CANDIDATE_NAME]]"
    result = client.complete(
        "test",
        "fast",
        [{"role": "user", "content": "Write to testa.candidato@example.com, +91 90000 00001"}],
        50,
    )
    sent = json.dumps(provider.calls[0]["messages"])
    assert "example.com" not in sent and "90000" not in sent
    assert "[[CANDIDATE_EMAIL]]" in sent
    assert result.text == "Hello Testa Candidato Q"
    assert result.model == "gemini-3.5-flash-lite"
    [row] = _rows(conn)
    assert (row["sent"], row["ok"], row["prompt_tokens"]) == (1, 1, 7)


def test_leak_sends_nothing(client, provider, conn, monkeypatch):
    monkeypatch.setattr(client.redactor, "redact", lambda text: text)
    with pytest.raises(RedactionLeak):
        client.complete("test", "fast", [{"role": "user", "content": "I am Testa Candidato Q"}], 50)
    assert provider.calls == []
    [row] = _rows(conn)
    assert (row["error_code"], row["sent"]) == ("redaction_leak", 0)


def test_daily_429_marks_model_and_falls_through(client, provider, conn):
    provider.plan = {"gemini-3.5-flash-lite": [_rate_limit(DAILY_429)]}
    result = client.complete("test", "fast", [{"role": "user", "content": "hi"}], 50)
    assert result.model == "gemini-3.1-flash-lite"
    assert quota.exhausted_until(conn, "gemini-3.5-flash-lite", NOW) == quota.next_pacific_midnight(NOW)


def test_all_models_exhausted_pauses(client, provider, conn):
    provider.plan = {m: [_rate_limit(DAILY_429)] for m in client.llm.fast_models}
    with pytest.raises(llm.QuotaExhausted):
        client.complete("test", "fast", [{"role": "user", "content": "hi"}], 50)
    assert db.get_flag(conn, "llm_paused_until") == db.to_iso(quota.next_pacific_midnight(NOW))
    calls_before = len(provider.calls)
    with pytest.raises(llm.LLMPaused):
        client.complete("test", "strong", [{"role": "user", "content": "hi"}], 50)
    assert len(provider.calls) == calls_before


def test_minute_429_backs_off_then_moves_on(client, provider):
    provider.plan = {"gemini-3.5-flash-lite": [_rate_limit(MINUTE_429)] * 3}
    result = client.complete("test", "fast", [{"role": "user", "content": "hi"}], 50)
    assert client.sleeps == [5, 10]
    assert result.model == "gemini-3.1-flash-lite"


def test_all_minute_limited_is_busy_not_paused(client, provider, conn):
    provider.plan = {m: [_rate_limit(MINUTE_429)] * 3 for m in client.llm.fast_models}
    with pytest.raises(llm.LLMBusy):
        client.complete("test", "fast", [{"role": "user", "content": "hi"}], 50)
    assert db.get_flag(conn, "llm_paused_until") is None


def test_local_daily_count_stops_before_provider(client, provider, conn, settings):
    limit = settings.llm.limits["gemini-3.5-flash-lite"].rpd
    for _ in range(int(limit * settings.llm.quota_headroom)):
        conn.execute(
            "INSERT INTO llm_calls(stage, tier, model, sent, ok, created_at) VALUES ('t','fast',?,1,1,?)",
            ("gemini-3.5-flash-lite", db.to_iso(NOW - timedelta(hours=2))),
        )
    result = client.complete("test", "fast", [{"role": "user", "content": "hi"}], 50)
    assert result.model == "gemini-3.1-flash-lite"
    assert all(c["model"] != "gemini/gemini-3.5-flash-lite" for c in provider.calls)
    assert quota.exhausted_until(conn, "gemini-3.5-flash-lite", NOW) is not None


class Answer(BaseModel):
    verdict: str


def test_json_schema_retries_invalid_json(client, provider):
    provider.plan = {"gemini-3.5-flash-lite": ["not json", '{"verdict": "ok for [[CANDIDATE_FIRST_NAME]]"}']}
    result = client.complete("test", "fast", [{"role": "user", "content": "hi"}], 50, json_schema=Answer)
    assert result.data == Answer(verdict="ok for Testa")
    assert provider.calls[0]["response_format"] is Answer


def test_auth_error_stops(client, provider):
    provider.plan = {
        "gemini-3.5-flash-lite": [
            llm.litellm.AuthenticationError(message="bad key", llm_provider="gemini", model="m")
        ]
    }
    with pytest.raises(llm.LLMError, match="API key"):
        client.complete("test", "fast", [{"role": "user", "content": "hi"}], 50)


def test_route_gemini_and_local_endpoints(client, monkeypatch):
    from agent.config import LocalEndpoint

    # Bare model routes to gemini
    model, base, key, timeout = client._route("gemini-3.5-flash-lite")
    assert model == "gemini/gemini-3.5-flash-lite"
    assert base is None
    assert key == "test-key"
    assert timeout == client.llm.request_timeout_s

    # Local endpoint configured
    client.llm.local_endpoints["rohit-test"] = LocalEndpoint(
        api_base="https://test.instabox.tech/v1",
        token_secret="test_secret",  # pragma: allowlist secret
        timeout_s=45,
    )
    monkeypatch.setenv("JOB_AGENT_SECRET_TEST_SECRET", "bearer-12345")

    model, base, key, timeout = client._route("rohit-test/qwen2.5:3b")
    assert model == "openai/qwen2.5:3b"
    assert base == "https://test.instabox.tech/v1"
    assert key == "bearer-12345"
    assert timeout == 45

    # Unknown endpoint raises
    with pytest.raises(llm.LLMError, match="unknown local endpoint"):
        client._route("nonexistent/model")


def test_prewarm_skips_missing_secret(client):
    from agent.config import LocalEndpoint

    client.llm.local_endpoints["rohit-unauthed"] = LocalEndpoint(
        api_base="https://unauthed.instabox.tech/v1",
        token_secret="missing_secret_key",  # pragma: allowlist secret
        timeout_s=10,
    )
    status = client.prewarm()
    assert status.get("rohit-unauthed") is False
    assert "rohit-unauthed" in client._skipped_endpoints


def test_finish_reason_length_moves_to_next_model(client, provider, monkeypatch):
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "gemini/gemini-3.5-flash-lite":
            # Returns truncated response
            resp = _response("truncated json...")
            resp.choices[0].finish_reason = "length"
            return resp
        return _response("valid fallback response")

    monkeypatch.setattr(llm.litellm, "completion", fake_completion)

    res = client.complete("test", "fast", [{"role": "user", "content": "hi"}], max_tokens=100)
    assert res is not None
    assert res.text == "valid fallback response"
    assert res.model == "gemini-3.1-flash-lite"
    assert len(calls) == 2


def test_permission_denied_endpoint_skips_and_falls_back(client, monkeypatch):
    import httpx

    from agent.config import LocalEndpoint

    client.llm.local_endpoints["rohit-denied"] = LocalEndpoint(
        api_base="https://denied.instabox.tech/v1",
        token_secret="tok_secret",  # pragma: allowlist secret
        timeout_s=10,
    )
    monkeypatch.setenv("JOB_AGENT_SECRET_TOK_SECRET", "fake-token")

    def fake_completion(**kwargs):
        if kwargs.get("model") == "openai/qwen2.5:3b":
            exc_cls = getattr(llm.litellm, "PermissionDeniedError", llm.litellm.AuthenticationError)
            mock_resp = httpx.Response(403, request=httpx.Request("POST", "https://denied.instabox.tech/v1"))
            try:
                raise exc_cls("403 Forbidden", response=mock_resp, llm_provider="openai", model="m")
            except TypeError:
                raise exc_cls("403 Forbidden", llm_provider="openai", model="m") from None
        return _response("ok from fallback")

    monkeypatch.setattr(llm.litellm, "completion", fake_completion)
    object.__setattr__(client.llm, "fast_models", ["rohit-denied/qwen2.5:3b", "gemini-3.5-flash-lite"])

    res = client.complete("test", "fast", [{"role": "user", "content": "hi"}], max_tokens=100)
    assert res is not None
    assert res.text == "ok from fallback"
    assert client.is_endpoint_skipped("rohit-denied") is True


def test_prewarm_hang_marks_skipped_and_gets_zero_calls(client, monkeypatch):
    import requests

    from agent.config import LocalEndpoint

    client.llm.local_endpoints["rohit-hang"] = LocalEndpoint(
        api_base="https://hang.instabox.tech/v1",
        token_secret="hang_secret",  # pragma: allowlist secret
        timeout_s=5,
    )
    monkeypatch.setenv("JOB_AGENT_SECRET_HANG_SECRET", "fake-token")

    def fake_get(url, **kwargs):
        raise requests.Timeout("mock timeout")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(client, "_sleep", lambda s: None)

    status = client.prewarm(timeout_total_s=0.1)
    assert status.get("rohit-hang") is False
    assert client.is_endpoint_skipped("rohit-hang") is True

    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        return _response("ok from fallback")

    monkeypatch.setattr(llm.litellm, "completion", fake_completion)
    object.__setattr__(client.llm, "fast_models", ["rohit-hang/qwen2.5:3b", "gemini-3.5-flash-lite"])
    res = client.complete("test", "fast", [{"role": "user", "content": "hi"}], max_tokens=100)
    assert res is not None
    assert "openai/qwen2.5:3b" not in calls
    assert calls == ["gemini/gemini-3.5-flash-lite"]


def test_prewarm_401_403_skips_endpoint(client, monkeypatch):
    import requests

    from agent.config import LocalEndpoint

    client.llm.local_endpoints["rohit-auth-fail"] = LocalEndpoint(
        api_base="https://authfail.instabox.tech/v1",
        token_secret="auth_secret",  # pragma: allowlist secret
        timeout_s=5,
    )
    monkeypatch.setenv("JOB_AGENT_SECRET_AUTH_SECRET", "fake-token")

    class MockResponse:
        status_code = 401
        headers = {}

    monkeypatch.setattr(requests, "get", lambda url, **kwargs: MockResponse())
    monkeypatch.setattr(client, "_sleep", lambda s: None)

    status = client.prewarm(timeout_total_s=1.0)
    assert status.get("rohit-auth-fail") is False
    assert client.is_endpoint_skipped("rohit-auth-fail") is True


def test_cloud_providers_route_and_missing_key_skip(client, monkeypatch):
    from agent.config import ModelLimit

    # Set up models in settings limits
    client.llm.limits["groq/qwen/qwen3.8-27b"] = ModelLimit(rpm=30, rpd=1000)
    client.llm.limits["cloudflare/@cf/meta/llama-3.1-8b-instruct"] = ModelLimit(rpm=30, rpd=1000)
    client.llm.limits["openrouter/liquid/lfm-2.5-2.6b:free"] = ModelLimit(rpm=10, rpd=50)

    # 1. With groq key set
    monkeypatch.setenv("JOB_AGENT_SECRET_GROQ_API_KEY", "fake-groq-key")
    routed, base, key, _ = client._route("groq/qwen/qwen3.8-27b")
    assert routed == "groq/qwen/qwen3.8-27b"
    assert key == "fake-groq-key"

    # 2. With cloudflare key set
    monkeypatch.setenv("JOB_AGENT_SECRET_CLOUDFLARE_API_TOKEN", "fake-cf-token")
    monkeypatch.setenv("JOB_AGENT_SECRET_CLOUDFLARE_ACCOUNT_ID", "fake-cf-account")
    routed, base, key, _ = client._route("cloudflare/@cf/meta/llama-3.1-8b-instruct")
    assert routed == "cloudflare/@cf/meta/llama-3.1-8b-instruct"
    assert base == "https://api.cloudflare.com/client/v4/accounts/fake-cf-account/ai/run"
    assert key == "fake-cf-token"

    # 3. Missing key skips rung without crashing
    monkeypatch.delenv("JOB_AGENT_SECRET_OPENROUTER_API_KEY", raising=False)
    object.__setattr__(
        client.llm,
        "fast_models",
        ["openrouter/liquid/lfm-2.5-2.6b:free", "gemini-3.5-flash-lite"],
    )

    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        return _response("ok from fallback")

    monkeypatch.setattr(llm.litellm, "completion", fake_completion)
    res = client.complete("test", "fast", [{"role": "user", "content": "hello"}], max_tokens=100)
    assert res is not None
    # openrouter was skipped because no key, fell through to gemini
    assert calls == ["gemini/gemini-3.5-flash-lite"]
