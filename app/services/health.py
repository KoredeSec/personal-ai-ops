"""System health snapshot for /status (contains no secrets)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.core.config import Settings
from app.core.timeutils import utcnow
from app.database.migrate import schema_status
from app.database.models import DeliveryStatus, NotificationLog, Reminder, ReminderStatus
from app.database.session import check_database, session_scope
from app.services.backup import latest_backup_time
from app.services.heartbeat import heartbeat_age_seconds
from app.services.llm_store import DatabaseUsageStore

logger = logging.getLogger(__name__)

FAILURE_WINDOW = timedelta(days=7)
BACKUP_MAX_AGE = timedelta(hours=36)


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
    heartbeat_age: float | None = None
    heartbeat_max_age: int = 300
    last_backup: datetime | None = None
    backups_expected: bool = True
    failed_deliveries: int = 0
    failed_reminders: int = 0
    extra_problems: list[str] = field(default_factory=list)

    @property
    def schema_ok(self) -> bool:
        return self.schema_current is not None and self.schema_current == self.schema_head

    @property
    def heartbeat_ok(self) -> bool:
        return self.heartbeat_age is not None and self.heartbeat_age <= self.heartbeat_max_age

    @property
    def backup_ok(self) -> bool:
        if not self.backups_expected:
            return True
        return self.last_backup is not None and utcnow() - self.last_backup < BACKUP_MAX_AGE

    def problems(self) -> list[str]:
        out = []
        if not self.database_ok:
            out.append("Database unreachable")
        if self.database_ok and not self.schema_ok:
            out.append("Schema out of date (run alembic upgrade head)")
        if not self.gemini_configured:
            out.append("Gemini key missing (AI features off)")
        if not self.heartbeat_ok:
            out.append("Scheduler heartbeat stale")
        if not self.backup_ok:
            out.append("No backup in the last 36h")
        if self.failed_deliveries or self.failed_reminders:
            out.append(f"{self.failed_reminders} reminder(s) / {self.failed_deliveries} notification(s) "
                       "failed in the last 7 days")
        return out + self.extra_problems


def collect_health(settings: Settings) -> HealthReport:
    try:
        db_ok = check_database()
    except Exception:
        logger.exception("Database health check failed")
        db_ok = False
    current = head = None
    used = None
    failed_deliveries = failed_reminders = 0
    if db_ok:
        try:
            current, head = schema_status()
            used = DatabaseUsageStore().requests_today()
            since = utcnow() - FAILURE_WINDOW  # old failures should not keep /status red forever
            with session_scope() as s:
                failed_deliveries = s.scalar(
                    select(func.count()).select_from(NotificationLog).where(
                        NotificationLog.status == DeliveryStatus.FAILED, NotificationLog.updated_at >= since)) or 0
                failed_reminders = s.scalar(
                    select(func.count()).select_from(Reminder).where(
                        Reminder.status == ReminderStatus.FAILED, Reminder.updated_at >= since)) or 0
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
        heartbeat_age=heartbeat_age_seconds(settings.heartbeat_file),
        heartbeat_max_age=settings.heartbeat_max_age_seconds,
        last_backup=latest_backup_time(settings),
        backups_expected=settings.is_sqlite,
        failed_deliveries=failed_deliveries,
        failed_reminders=failed_reminders,
    )
