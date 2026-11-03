"""Database-backed UsageStore for the Gemini client."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select

from app.core.llm import UsageRecord
from app.core.timeutils import local_day_bounds_utc, now_local, utcnow
from app.database.models import AgentExecutionLog, ExecutionStatus, LLMCacheEntry
from app.database.session import session_scope


class DatabaseUsageStore:
    def requests_today(self) -> int:
        """Real (non-cached) Gemini calls attempted during the local day."""
        start, end = local_day_bounds_utc(now_local().date())
        with session_scope() as s:
            total = s.scalar(
                select(func.coalesce(func.sum(AgentExecutionLog.attempts), 0)).where(
                    AgentExecutionLog.created_at >= start,
                    AgentExecutionLog.created_at < end,
                    AgentExecutionLog.model.is_not(None),
                    AgentExecutionLog.cache_hit.is_(False),
                    # budget refusals never reached the API
                    (AgentExecutionLog.error_type.is_(None))
                    | (AgentExecutionLog.error_type != "LLMBudgetExceededError"),
                )
            )
        return int(total or 0)

    def record(self, record: UsageRecord) -> None:
        with session_scope() as s:
            s.add(AgentExecutionLog(
                user_id=record.user_id,
                agent=record.agent,
                action=record.action,
                status=ExecutionStatus.SUCCESS if record.success else ExecutionStatus.ERROR,
                model=record.model,
                prompt_tokens=record.prompt_tokens,
                output_tokens=record.output_tokens,
                total_tokens=record.total_tokens,
                latency_ms=record.latency_ms,
                attempts=record.attempts,
                cache_hit=record.cache_hit,
                error_type=record.error_type,
                error_message=record.error_message,
            ))

    def cache_get(self, key: str) -> str | None:
        with session_scope() as s:
            entry = s.scalar(select(LLMCacheEntry).where(LLMCacheEntry.cache_key == key))
            if entry is None:
                return None
            if entry.expires_at <= utcnow():
                s.delete(entry)
                return None
            return entry.response_text

    def cache_set(self, key: str, model: str, text: str, ttl_hours: int) -> None:
        if ttl_hours <= 0:
            return
        expires = utcnow() + timedelta(hours=ttl_hours)
        with session_scope() as s:
            entry = s.scalar(select(LLMCacheEntry).where(LLMCacheEntry.cache_key == key))
            if entry is None:
                s.add(LLMCacheEntry(cache_key=key, model=model, response_text=text, expires_at=expires))
            else:
                entry.response_text = text
                entry.model = model
                entry.expires_at = expires


def purge_expired_cache() -> int:
    with session_scope() as s:
        result = s.execute(delete(LLMCacheEntry).where(LLMCacheEntry.expires_at <= utcnow()))
        return result.rowcount or 0
