import asyncio

import httpx
import pytest
from google.genai import errors
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import Settings
from app.core.llm import (
    GeminiClient,
    LLMAuthError,
    LLMBudgetExceededError,
    LLMError,
    LLMNotConfiguredError,
    LLMQuotaExceededError,
    LLMRequest,
    LLMResponseError,
    LLMResult,
    LLMUnavailableError,
    UsageRecord,
)
from app.database.models import AgentExecutionLog, ExecutionStatus
from app.database.session import session_scope
from app.services.llm_store import DatabaseUsageStore


class MemoryStore:
    def __init__(self, used=0):
        self.used = used
        self.records: list[UsageRecord] = []
        self.cache: dict[str, str] = {}

    def requests_today(self):
        return self.used

    def record(self, r):
        self.records.append(r)

    def cache_get(self, k):
        return self.cache.get(k)

    def cache_set(self, k, model, text, ttl):
        self.cache[k] = text


class FakeBackend:
    """Replays a script of results/exceptions."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[LLMRequest] = []

    async def __call__(self, req):
        self.requests.append(req)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return LLMResult(text=item, model=req.model, prompt_tokens=5, output_tokens=2, total_tokens=7)


def mk(backend, store=None, **overrides):
    s = Settings(_env_file=None, **overrides)
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    client = GeminiClient(s, store=store if store is not None else MemoryStore(), backend=backend, sleep=fake_sleep)
    return client, sleeps


def api_error(code, status, details=None):
    body = {"error": {"code": code, "status": status, "message": "x", "details": details or []}}
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, body)


async def test_success_records_usage_and_uses_configured_models():
    store = MemoryStore()
    be = FakeBackend("hello", "ok")
    c, _ = mk(be, store, gemini_model="model-main", gemini_model_light="model-lite")
    r = await c.generate("hi", agent="cos", action="t")
    await c.generate("hi", tier="light")
    assert r.text == "hello" and r.total_tokens == 7 and r.attempts == 1
    assert [q.model for q in be.requests] == ["model-main", "model-lite"]
    assert store.records[0].success and store.records[0].agent == "cos"


async def test_retries_transient_server_errors_with_backoff():
    be = FakeBackend(api_error(503, "UNAVAILABLE"), httpx.ReadTimeout("t"), "ok")
    c, sleeps = mk(be)
    r = await c.generate("hi")
    assert r.text == "ok" and r.attempts == 3
    assert len(sleeps) == 2 and sleeps[0] < sleeps[1]


async def test_daily_quota_exhausted_is_not_retried():
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]
    store = MemoryStore()
    be = FakeBackend(api_error(429, "RESOURCE_EXHAUSTED", details))
    c, sleeps = mk(be, store)
    with pytest.raises(LLMQuotaExceededError) as ei:
        await c.generate("hi")
    assert sleeps == [] and len(be.requests) == 1
    assert "quota" in ei.value.user_message.lower()
    assert not store.records[-1].success


async def test_per_minute_rate_limit_honours_server_retry_delay():
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]
    be = FakeBackend(api_error(429, "RESOURCE_EXHAUSTED", details), "ok")
    c, sleeps = mk(be)
    assert (await c.generate("hi")).text == "ok"
    assert 7 <= sleeps[0] <= 8


async def test_excessive_server_retry_delay_fails_fast():
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}]
    be = FakeBackend(api_error(429, "RESOURCE_EXHAUSTED", details))
    c, sleeps = mk(be)
    with pytest.raises(LLMQuotaExceededError):
        await c.generate("hi")
    assert sleeps == []


async def test_persistent_rate_limit_maps_to_quota_error():
    be = FakeBackend(*[api_error(429, "RESOURCE_EXHAUSTED")] * 3)
    c, sleeps = mk(be, gemini_max_retries=2)
    with pytest.raises(LLMQuotaExceededError):
        await c.generate("hi")
    assert len(be.requests) == 3 and len(sleeps) == 2


async def test_persistent_outage_maps_to_unavailable():
    be = FakeBackend(*[api_error(500, "INTERNAL")] * 2)
    c, _ = mk(be, gemini_max_retries=1)
    with pytest.raises(LLMUnavailableError):
        await c.generate("hi")


async def test_client_error_not_retried():
    be = FakeBackend(api_error(400, "INVALID_ARGUMENT"))
    c, sleeps = mk(be)
    with pytest.raises(LLMError):
        await c.generate("hi")
    assert sleeps == [] and len(be.requests) == 1


async def test_hung_backend_hits_timeout():
    async def hang(_req):
        await asyncio.sleep(10)

    c, _ = mk(hang, gemini_max_retries=1)
    c.hard_timeout_seconds = 0.05
    with pytest.raises(LLMUnavailableError):
        await c.generate("hi")


async def test_local_daily_budget_blocks_before_calling_api():
    be = FakeBackend("never")
    c, _ = mk(be, MemoryStore(used=5), gemini_daily_request_budget=5)
    with pytest.raises(LLMBudgetExceededError):
        await c.generate("hi")
    assert be.requests == []


async def test_cache_hit_skips_backend_and_budget():
    store = MemoryStore()
    be = FakeBackend("first")
    c, _ = mk(be, store)
    a = await c.generate("summarise X", cache_ttl_hours=24)
    store.used = 10**6  # budget exhausted, but cache still serves
    b = await c.generate("summarise X", cache_ttl_hours=24)
    assert a.text == b.text == "first" and b.cache_hit and len(be.requests) == 1


async def test_uncached_by_default():
    be = FakeBackend("a", "b")
    c, _ = mk(be)
    await c.generate("same")
    assert (await c.generate("same")).text == "b"


async def test_prompt_is_capped():
    be = FakeBackend("ok")
    c, _ = mk(be, gemini_max_prompt_chars=1000)
    await c.generate("x" * 5000)
    assert len(be.requests[0].prompt) < 1100 and be.requests[0].prompt.endswith("[prompt truncated]")


class Item(BaseModel):
    title: str
    score: int


async def test_generate_json_validates_and_retries_once_on_bad_json():
    be = FakeBackend("not json", '```json\n{"title": "t", "score": 3}\n```')
    c, _ = mk(be)
    item = await c.generate_json("x", Item)
    assert item == Item(title="t", score=3)
    assert be.requests[0].response_schema is Item


async def test_generate_json_gives_up_after_two_bad_responses():
    be = FakeBackend('{"title": 1}', "{}")
    c, _ = mk(be)
    with pytest.raises(LLMResponseError):
        await c.generate_json("x", Item)


async def test_not_configured():
    c = GeminiClient(Settings(_env_file=None, gemini_api_key=""))
    assert not c.configured
    with pytest.raises(LLMNotConfiguredError):
        await c.generate("hi")


async def test_error_messages_are_redacted_in_usage_log():
    key = "AIzaTEST_KEY_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    store = MemoryStore()
    be = FakeBackend(api_error(400, "INVALID_ARGUMENT", [{"leak": key}]))
    c, _ = mk(be, store)
    with pytest.raises(LLMError):
        await c.generate("hi")
    assert key not in (store.records[-1].error_message or "")


async def test_database_usage_store_end_to_end(db):
    store = DatabaseUsageStore()
    be = FakeBackend("cached-answer", api_error(400, "INVALID_ARGUMENT"))
    c, _ = mk(be, store, gemini_daily_request_budget=3)
    await c.generate("p", cache_ttl_hours=1, user_id=None)
    await c.generate("p", cache_ttl_hours=1)  # cache hit
    with pytest.raises(LLMError):
        await c.generate("q")
    assert store.requests_today() == 2  # cache hit not counted
    with session_scope() as s:
        rows = s.scalars(select(AgentExecutionLog).order_by(AgentExecutionLog.id)).all()
        assert [(r.status, r.cache_hit) for r in rows] == [
            (ExecutionStatus.SUCCESS, False), (ExecutionStatus.SUCCESS, True), (ExecutionStatus.ERROR, False)]
        assert rows[0].total_tokens == 7


async def test_invalid_api_key_maps_to_auth_error_without_retry():
    err = errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                             "message": "API key not valid. Please pass a valid API key."}})
    be = FakeBackend(err)
    c, sleeps = mk(be)
    with pytest.raises(LLMAuthError) as ei:
        await c.generate("hi")
    assert sleeps == [] and "GEMINI_API_KEY" in ei.value.user_message
