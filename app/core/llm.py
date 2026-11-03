"""Gemini client abstraction.

Rules enforced here (so callers don't have to remember them):
  * credentials come only from settings; they are never logged
  * every call has a timeout and bounded retries with exponential backoff
  * per-minute rate limits are retried; exhausted daily quota is not
  * a local daily request budget protects the free tier
  * prompts are size-capped
  * every call (success, failure, cache hit) is recorded for observability
  * failures surface as LLMError subclasses carrying a safe user message

Call this module only when reasoning / summarisation / classification / NLU
is genuinely needed. Deterministic work must stay in plain Python.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.logging import redact

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 30.0
MAX_SERVER_RETRY_DELAY = 60.0


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class LLMError(Exception):
    user_message = "The AI service failed to respond. Please try again shortly."


class LLMNotConfiguredError(LLMError):
    user_message = "AI features are disabled: GEMINI_API_KEY is not configured."


class LLMAuthError(LLMError):
    user_message = "Gemini rejected the API key. Check GEMINI_API_KEY (https://aistudio.google.com/apikey)."


class LLMQuotaExceededError(LLMError):
    user_message = (
        "The Gemini quota is exhausted for now. Deterministic features (tasks, "
        "reminders, lists) still work; AI features will resume when quota resets."
    )


class LLMBudgetExceededError(LLMError):
    user_message = (
        "Today's configured AI request budget has been used. Deterministic features "
        "still work. Raise GEMINI_DAILY_REQUEST_BUDGET if this is too low."
    )


class LLMUnavailableError(LLMError):
    user_message = "The AI service is temporarily unavailable. Please try again in a few minutes."


class LLMResponseError(LLMError):
    user_message = "The AI returned an unusable response. Please rephrase or try again."


# --------------------------------------------------------------------------
# Data types & protocols
# --------------------------------------------------------------------------
@dataclass
class LLMRequest:
    model: str
    prompt: str
    system_instruction: str | None
    temperature: float
    max_output_tokens: int
    response_schema: type[BaseModel] | None = None


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int = 0
    attempts: int = 1
    cache_hit: bool = False


@dataclass
class UsageRecord:
    agent: str
    action: str
    model: str
    success: bool
    user_id: int | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    attempts: int = 1
    cache_hit: bool = False
    error_type: str | None = None
    error_message: str | None = None


class UsageStore(Protocol):
    """Persistence for usage logs, the daily budget and the response cache."""

    def requests_today(self) -> int: ...
    def record(self, record: UsageRecord) -> None: ...
    def cache_get(self, key: str) -> str | None: ...
    def cache_set(self, key: str, model: str, text: str, ttl_hours: int) -> None: ...


Backend = Callable[[LLMRequest], Awaitable[LLMResult]]


# --------------------------------------------------------------------------
# Default backend: official google-genai SDK
# --------------------------------------------------------------------------
class GenaiBackend:
    def __init__(self, api_key: str, timeout_seconds: float, thinking_budget: int | None = None):
        from google import genai
        from google.genai import types

        self._types = types
        self._thinking_budget = thinking_budget
        # SDK-level retries are left off: retries are handled (and logged) by
        # GeminiClient so there is exactly one retry policy.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )

    async def __call__(self, req: LLMRequest) -> LLMResult:
        types = self._types
        config_kwargs: dict[str, Any] = {
            "temperature": req.temperature,
            "max_output_tokens": req.max_output_tokens,
        }
        if req.system_instruction:
            config_kwargs["system_instruction"] = req.system_instruction
        if req.response_schema is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = req.response_schema
        if self._thinking_budget is not None:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=self._thinking_budget)

        response = await self._client.aio.models.generate_content(
            model=req.model,
            contents=req.prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        text = response.text
        if not text:
            reason = None
            if response.candidates:
                reason = getattr(response.candidates[0], "finish_reason", None)
            raise LLMResponseError(f"Empty response from model (finish_reason={reason})")
        usage = response.usage_metadata
        return LLMResult(
            text=text,
            model=req.model,
            prompt_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            total_tokens=getattr(usage, "total_token_count", None),
        )


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", re.IGNORECASE)


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def is_daily_quota_exhausted(exc: BaseException) -> bool:
    """429s whose QuotaFailure names a per-day quota will not clear by retrying."""
    if _status_code(exc) != 429:
        return False
    details = json.dumps(getattr(exc, "details", None) or str(exc), default=str)
    return "PerDay" in details or "per day" in details.lower()


def server_retry_delay(exc: BaseException) -> float | None:
    details = json.dumps(getattr(exc, "details", None) or str(exc), default=str)
    match = _RETRY_DELAY_RE.search(details)
    return float(match.group(1)) if match else None


def is_auth_error(exc: BaseException) -> bool:
    code = _status_code(exc)
    if code in (401, 403):
        return True
    return code == 400 and "API key not valid" in str(exc)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, LLMError):
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, asyncio.TimeoutError, TimeoutError)):
        return True
    code = _status_code(exc)
    return code in RETRYABLE_STATUS


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
class GeminiClient:
    def __init__(
        self,
        settings: Settings,
        store: UsageStore | None = None,
        backend: Backend | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.settings = settings
        self.store = store
        self._backend = backend
        self._sleep = sleep
        self._secrets = settings.secret_values()
        # Outer safety net in case the SDK's own HTTP timeout does not fire.
        self.hard_timeout_seconds = settings.gemini_timeout_seconds + 5

    # -- configuration ---------------------------------------------------
    @property
    def configured(self) -> bool:
        return self._backend is not None or bool(
            self.settings.gemini_api_key and self.settings.gemini_api_key.get_secret_value()
        )

    def _get_backend(self) -> Backend:
        if self._backend is None:
            if not self.configured:
                raise LLMNotConfiguredError("GEMINI_API_KEY not set")
            self._backend = GenaiBackend(
                api_key=self.settings.gemini_api_key.get_secret_value(),  # type: ignore[union-attr]
                timeout_seconds=self.settings.gemini_timeout_seconds,
                thinking_budget=self.settings.gemini_thinking_budget,
            )
        return self._backend

    def model_for(self, tier: str) -> str:
        return self.settings.gemini_model_light if tier == "light" else self.settings.gemini_model

    # -- public API ------------------------------------------------------
    async def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        tier: str = "primary",
        model: str | None = None,
        temperature: float = 0.3,
        max_output_tokens: int | None = None,
        cache_ttl_hours: int | None = None,
        agent: str = "system",
        action: str = "generate",
        user_id: int | None = None,
        _schema: type[BaseModel] | None = None,
    ) -> LLMResult:
        req = LLMRequest(
            model=model or self.model_for(tier),
            prompt=self._cap_prompt(prompt),
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens or self.settings.gemini_max_output_tokens,
            response_schema=_schema,
        )
        cache_key = self._cache_key(req) if cache_ttl_hours else None
        if cache_key and self.store:
            cached = self.store.cache_get(cache_key)
            if cached is not None:
                self._record(UsageRecord(agent, action, req.model, True, user_id=user_id,
                                         latency_ms=0, cache_hit=True))
                return LLMResult(text=cached, model=req.model, cache_hit=True)

        if self.store and self.store.requests_today() >= self.settings.gemini_daily_request_budget:
            self._record(UsageRecord(agent, action, req.model, False, user_id=user_id,
                                     error_type="LLMBudgetExceededError"))
            raise LLMBudgetExceededError("Local daily request budget reached")

        backend = self._get_backend()
        started = time.monotonic()
        attempts = 0
        last_exc: BaseException | None = None
        max_attempts = self.settings.gemini_max_retries + 1
        while attempts < max_attempts:
            attempts += 1
            try:
                result = await asyncio.wait_for(backend(req), timeout=self.hard_timeout_seconds)
            except BaseException as exc:  # noqa: BLE001 - classified below
                if isinstance(exc, asyncio.CancelledError):
                    raise
                last_exc = exc
                if is_daily_quota_exhausted(exc):
                    self._fail(req, agent, action, user_id, started, attempts, exc)
                    raise LLMQuotaExceededError("Daily quota exhausted") from exc
                if not is_retryable(exc) or attempts >= max_attempts:
                    break
                delay = self._backoff(attempts, exc)
                if delay is None:  # server asked for longer than we are willing to wait
                    self._fail(req, agent, action, user_id, started, attempts, exc)
                    raise LLMQuotaExceededError("Rate limited for an extended period") from exc
                logger.warning(
                    "Gemini call failed (attempt %d/%d, %s); retrying in %.1fs",
                    attempts, max_attempts, type(exc).__name__, delay,
                )
                await self._sleep(delay)
                continue

            result.latency_ms = int((time.monotonic() - started) * 1000)
            result.attempts = attempts
            self._record(UsageRecord(
                agent, action, req.model, True, user_id=user_id,
                prompt_tokens=result.prompt_tokens, output_tokens=result.output_tokens,
                total_tokens=result.total_tokens, latency_ms=result.latency_ms, attempts=attempts,
            ))
            logger.info(
                "Gemini %s/%s model=%s tokens=%s latency=%dms attempts=%d",
                agent, action, req.model, result.total_tokens, result.latency_ms, attempts,
            )
            if cache_key and self.store:
                self.store.cache_set(cache_key, req.model, result.text, cache_ttl_hours or 0)
            return result

        assert last_exc is not None
        self._fail(req, agent, action, user_id, started, attempts, last_exc)
        if isinstance(last_exc, LLMError):
            raise last_exc
        if is_auth_error(last_exc):
            raise LLMAuthError("API key rejected") from last_exc
        if _status_code(last_exc) == 429:
            raise LLMQuotaExceededError("Rate limit persisted after retries") from last_exc
        if is_retryable(last_exc):
            raise LLMUnavailableError(f"Gemini unavailable after {attempts} attempts") from last_exc
        raise LLMError(f"Gemini request failed: {type(last_exc).__name__}") from last_exc

    async def generate_json(self, prompt: str, schema: type[T], **kwargs: Any) -> T:
        """Structured output validated against a Pydantic model.

        One extra attempt is made if the model returns invalid JSON.
        """
        for attempt in range(2):
            result = await self.generate(prompt, _schema=schema, **kwargs)
            try:
                return schema.model_validate_json(_strip_code_fences(result.text))
            except ValidationError as exc:
                logger.warning("Gemini returned invalid JSON for %s (attempt %d)", schema.__name__, attempt + 1)
                kwargs["cache_ttl_hours"] = None  # never reuse a bad cached answer
                last = exc
        raise LLMResponseError(f"Invalid structured response for {schema.__name__}") from last

    # -- internals -------------------------------------------------------
    def _cap_prompt(self, prompt: str) -> str:
        limit = self.settings.gemini_max_prompt_chars
        if len(prompt) <= limit:
            return prompt
        logger.warning("Prompt truncated from %d to %d chars", len(prompt), limit)
        return prompt[:limit] + "\n[prompt truncated]"

    @staticmethod
    def _cache_key(req: LLMRequest) -> str:
        payload = {
            "model": req.model,
            "system": req.system_instruction,
            "prompt": req.prompt,
            "temperature": req.temperature,
            "schema": req.response_schema.model_json_schema() if req.response_schema else None,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _backoff(attempt: int, exc: BaseException) -> float | None:
        server_delay = server_retry_delay(exc)
        if server_delay is not None:
            if server_delay > MAX_SERVER_RETRY_DELAY:
                return None
            return server_delay + random.uniform(0, 1)
        return min(MAX_BACKOFF_SECONDS, 2 ** (attempt - 1)) + random.uniform(0, 0.5)

    def _fail(self, req, agent, action, user_id, started, attempts, exc) -> None:
        msg = redact(str(exc), self._secrets)[:500]
        logger.error("Gemini %s/%s failed after %d attempt(s): %s: %s",
                     agent, action, attempts, type(exc).__name__, msg)
        self._record(UsageRecord(
            agent, action, req.model, False, user_id=user_id,
            latency_ms=int((time.monotonic() - started) * 1000), attempts=attempts,
            error_type=type(exc).__name__, error_message=msg,
        ))

    def _record(self, record: UsageRecord) -> None:
        if not self.store:
            return
        try:
            self.store.record(record)
        except Exception:  # logging must never break the request path
            logger.exception("Failed to record LLM usage")


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return text
