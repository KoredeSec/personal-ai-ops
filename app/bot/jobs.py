"""Scheduled jobs and owner alerts.

All proactive sends go through `services.notifications.deliver`, so a job
that runs twice (restart, overlap, retry) never double-notifies.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from datetime import time as dtime

from sqlalchemy import select
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from app.agents.chief_of_staff import agent, decisions, planning
from app.agents.chief_of_staff.preferences import get_prefs
from app.bot.formatting import chunk_message, esc
from app.core.config import Settings
from app.core.scheduler import JobSpec
from app.core.timeutils import now_local, utcnow
from app.database.models import User
from app.database.session import session_scope
from app.services import backup, heartbeat, notifications
from app.services.audit import AuditEvent, audit
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


def _owners() -> list[User]:
    with session_scope() as s:
        rows = s.scalars(select(User).where(User.is_active.is_(True))).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


async def alert_owner(application: Application, key: str, html: str, respect_quiet: bool = True) -> None:
    """Ops alert to every allowed user, deduplicated by `key`.

    System-initiated, so it honours quiet hours: during them nothing is sent
    and the failure appears in the next briefing instead (it is in the audit
    log as job_failed).
    """
    settings: Settings = application.bot_data["settings"]
    send = make_sender(application)
    known = {u.telegram_user_id: u for u in _owners()}
    for uid in settings.telegram_allowed_user_ids:
        user = known.get(uid)
        if respect_quiet and user is not None and get_prefs(user.id).in_quiet_hours(now_local()):
            logger.info("Alert %s held for quiet hours", key)
            continue
        await notifications.deliver(f"{key}:{uid}", "alert", lambda u=uid: send(u, html), settings)


async def on_job_error(name: str, exc: BaseException, context: ContextTypes.DEFAULT_TYPE) -> None:
    audit(AuditEvent.JOB_FAILED, None, job=name, error_type=type(exc).__name__)
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
    # Reminders you set yourself always fire, even in quiet hours: you chose the time.
    stats = await notifications.dispatch_due_reminders(
        make_sender(context.application), _settings(context),
        on_sent=lambda uid, rid: decisions.removed(uid, "reminder_delivered", f"reminder:{rid}"))
    if stats:
        logger.info("Reminder dispatch: %s", stats)


BRIEFING_GRACE = timedelta(hours=6)  # after downtime, still send a briefing up to 6h late


def _due_window(now_local_dt: datetime, at: dtime) -> bool:
    start = now_local_dt.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return start <= now_local_dt < start + BRIEFING_GRACE


async def planner_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the daily briefing and weekly review at each owner's configured time.

    Polls every few minutes rather than scheduling exact jobs, so changing
    the time in /settings needs no rescheduling and a restart never skips or
    repeats a delivery (keys: briefing:<user>:<date>, weekly:<user>:<iso-week>).
    """
    settings = _settings(context)
    llm = context.application.bot_data.get("llm")
    send = make_sender(context.application)
    local = now_local()
    allowed = set(settings.telegram_allowed_user_ids)
    for user in _owners():
        if user.telegram_user_id not in allowed:
            continue
        prefs = get_prefs(user.id)
        chat = user.telegram_chat_id or user.telegram_user_id
        ctx = agent.Ctx(user_id=user.id, prefs=prefs, llm=llm, now=utcnow(),
                        interactive_deadline=120.0)  # background: allow longer
        if _due_window(local, prefs.briefing_time):
            key = f"briefing:{user.id}:{local.date().isoformat()}"
            reply = agent.briefing_reply(ctx, local.date(), record=False)

            async def _send_briefing(reply=reply, ctx=ctx, day=local.date(), chat=chat):
                mid = await send(chat, reply.html)
                planning.record_briefing_decisions(ctx.user_id,
                                                   planning.build_briefing(ctx.user_id, ctx.prefs, day, ctx.now))
                return mid

            await notifications.deliver(key, "briefing", _send_briefing, settings, user_id=user.id)
        if local.weekday() == prefs.weekly_day and _due_window(local, prefs.weekly_time):
            iso = local.isocalendar()
            key = f"weekly:{user.id}:{iso.year}-W{iso.week:02d}"

            async def _send_weekly(ctx=ctx, chat=chat):
                return await send(chat, (await agent.weekly_reply(ctx)).html)

            await notifications.deliver(key, "weekly", _send_weekly, settings, user_id=user.id)


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
        JobSpec("planner", planner_job, interval_seconds=300, first_delay_seconds=20),
        JobSpec("backup", backup_job, daily_at=backup_at),
        JobSpec("housekeeping", housekeeping_job, interval_seconds=3600, first_delay_seconds=60),
    ]
