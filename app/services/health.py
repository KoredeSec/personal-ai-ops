"""System health snapshot for /status (contains no secrets)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.config import Settings
from app.database.migrate import schema_status
from app.database.session import check_database
from app.services.llm_store import DatabaseUsageStore

logger = logging.getLogger(__name__)


@dataclass
class HealthReport:
    database_ok: bool
    schema_current: str | None
    schema_head: str | None
    gemini_configured: bool
    gemini_model: str
    gemini_model_light: str
    llm_requests_today: int | None
    llm_daily_budget: int

    @property
    def schema_ok(self) -> bool:
        return self.schema_current is not None and self.schema_current == self.schema_head


def collect_health(settings: Settings) -> HealthReport:
    try:
        db_ok = check_database()
    except Exception:
        logger.exception("Database health check failed")
        db_ok = False
    current = head = None
    used = None
    if db_ok:
        try:
            current, head = schema_status()
            used = DatabaseUsageStore().requests_today()
        except Exception:
            logger.exception("Schema/usage health check failed")
    key = settings.gemini_api_key
    return HealthReport(
        database_ok=db_ok,
        schema_current=current,
        schema_head=head,
        gemini_configured=bool(key and key.get_secret_value()),
        gemini_model=settings.gemini_model,
        gemini_model_light=settings.gemini_model_light,
        llm_requests_today=used,
        llm_daily_budget=settings.gemini_daily_request_budget,
    )
