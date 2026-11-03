"""Background job registration.

Jobs run on python-telegram-bot's JobQueue (APScheduler under the hood), so
there is a single event loop and jobs can send Telegram messages directly.

Durability does not depend on the scheduler: reminders and digests are stored
in the database and polled by periodic jobs, so nothing is lost on restart and
the NotificationLog guarantees at-most-once delivery.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import time as dtime

from telegram.ext import Application, ContextTypes

logger = logging.getLogger(__name__)

JobCallback = Callable[[ContextTypes.DEFAULT_TYPE], Awaitable[None]]

# Applied to every job: don't pile up missed runs, never overlap a job with itself.
JOB_KWARGS = {"misfire_grace_time": 300, "coalesce": True, "max_instances": 1}


@dataclass(frozen=True)
class JobSpec:
    name: str
    callback: JobCallback
    interval_seconds: int | None = None  # repeating job
    daily_at: dtime | None = None  # local-time daily job (tz-aware time)
    first_delay_seconds: int = 10

    def __post_init__(self) -> None:
        if (self.interval_seconds is None) == (self.daily_at is None):
            raise ValueError(f"Job {self.name}: set exactly one of interval_seconds or daily_at")


ErrorCallback = Callable[[str, BaseException, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _safe(spec: JobSpec, on_error: ErrorCallback | None) -> JobCallback:
    async def runner(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await spec.callback(context)
        except Exception as exc:
            logger.exception("Scheduled job %s failed", spec.name)
            if on_error is not None:
                try:
                    await on_error(spec.name, exc, context)
                except Exception:
                    logger.exception("Job error callback failed for %s", spec.name)

    return runner


def register_jobs(application: Application, specs: list[JobSpec],
                  on_error: ErrorCallback | None = None) -> list[str]:
    jq = application.job_queue
    if jq is None:
        raise RuntimeError("JobQueue unavailable: install python-telegram-bot[job-queue]")
    names = []
    for spec in specs:
        for existing in jq.get_jobs_by_name(spec.name):
            existing.schedule_removal()
        if spec.interval_seconds is not None:
            jq.run_repeating(_safe(spec, on_error), interval=spec.interval_seconds, first=spec.first_delay_seconds,
                             name=spec.name, job_kwargs=JOB_KWARGS)
        else:
            jq.run_daily(_safe(spec, on_error), time=spec.daily_at, name=spec.name, job_kwargs=JOB_KWARGS)
        names.append(spec.name)
        logger.info("Registered job %s", spec.name)
    return names
