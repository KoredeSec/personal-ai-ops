"""Scheduled jobs and owner alerts.

All proactive sends go through `services.notifications.deliver`, so a job
that runs twice (restart, overlap, retry) never double-notifies.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from app.bot.formatting import chunk_message, esc
from app.core.config import Settings
from app.core.scheduler import JobSpec
from app.core.timeutils import now_local
from app.services import backup, heartbeat, notifications
from app.services.llm_store import purge_expired_cache

logger = logging.getLogger(__name__)


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def make_sender(application: Application):
    async def send_text(chat_id: int, html: str) -> int | None:
        last = None
        for part in chunk_message(html):
            last = await application.bot.send_message(chat_id, part, parse_mode=ParseMode.HTML)
        return last.message_id if last else None

    return send_text


async def alert_owner(application: Application, key: str, html: str) -> None:
    """Ops alert to every allowed user, deduplicated by `key`."""
    settings: Settings = application.bot_data["settings"]
    send = make_sender(application)
    for uid in settings.telegram_allowed_user_ids:
        await notifications.deliver(f"{key}:{uid}", "alert", lambda u=uid: send(u, html), settings)


async def on_job_error(name: str, exc: BaseException, context: ContextTypes.DEFAULT_TYPE) -> None:
    day = now_local().date().isoformat()  # at most one alert per job per day
    await alert_owner(
        context.application, f"alert:job:{name}:{day}",
        f"⚠️ <b>Background job failed</b>: {esc(name)}\n{esc(type(exc).__name__)}. "
        "Details are in the server logs. You will not be alerted again for this job today.",
    )


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(heartbeat.write_heartbeat, _settings(context).heartbeat_file)


async def reminder_dispatch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = await notifications.dispatch_due_reminders(make_sender(context.application), _settings(context))
    if stats:
        logger.info("Reminder dispatch: %s", stats)


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if not settings.is_sqlite:
        return
    await asyncio.to_thread(backup.create_backup, settings)  # raises -> on_job_error alerts


async def housekeeping_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    removed = purge_expired_cache()
    if removed:
        logger.info("Housekeeping: purged %d expired LLM cache entries", removed)


def build_jobs(settings: Settings) -> list[JobSpec]:
    hh, mm = (int(x) for x in settings.backup_time_local.split(":"))
    backup_at = datetime.now(settings.tz).replace(hour=hh, minute=mm, second=0, microsecond=0).timetz()
    return [
        JobSpec("heartbeat", heartbeat_job, interval_seconds=settings.heartbeat_interval_seconds,
                first_delay_seconds=1),
        JobSpec("reminder_dispatch", reminder_dispatch_job, interval_seconds=30, first_delay_seconds=5),
        JobSpec("backup", backup_job, daily_at=backup_at),
        JobSpec("housekeeping", housekeeping_job, interval_seconds=3600, first_delay_seconds=60),
    ]
