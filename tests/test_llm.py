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
