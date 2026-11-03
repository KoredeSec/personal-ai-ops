"""Crash-safe, idempotent notification delivery.

Every proactive message (reminder, briefing, digest, ops alert) goes through
`deliver()`, keyed by a stable dedupe_key such as "reminder:42" or
"briefing:2026-11-04".

Protocol (per key):
    claim   -> row SENDING (insert, or compare-and-swap from RETRY/stale SENDING)
    send    -> Telegram API call
    success -> SENT          (any later claim returns DUPLICATE)
    failure -> RETRY, or FAILED after `delivery_max_attempts`

Guarantees:
  * Concurrent or repeated calls for the same key never send twice while a
    claim is live (unique key + CAS on status/updated_at).
  * A process crash leaves a SENDING row; after `delivery_lease_seconds` it
    is reclaimed, so nothing is silently lost.
  * The only duplicate window is a crash *after* Telegram accepted the
    message but *before* SENT was recorded (milliseconds). We deliberately
    prefer a rare duplicate over a missed reminder.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.bot.formatting import esc
from app.core.config import Settings
from app.core.logging import redact
from app.core.timeutils import format_local, utcnow
from app.database.models import DeliveryStatus, NotificationLog, Reminder, ReminderStatus, User
from app.database.session import session_scope

logger = logging.getLogger(__name__)

SendFn = Callable[[], Awaitable[int | None]]  # returns telegram message_id


class Outcome(enum.StrEnum):
    SENT = "sent"
    DUPLICATE = "duplicate"  # already delivered earlier
    IN_PROGRESS = "in_progress"  # another live claim holds it
    RETRY = "retry"  # this attempt failed; will be retried
    GAVE_UP = "gave_up"  # max attempts reached


# --------------------------------------------------------------------------
# Generic ledger
# --------------------------------------------------------------------------
def _claim(key: str, kind: str, user_id: int | None, settings: Settings) -> Outcome | None:
    """Return None if this caller now holds the claim, else the reason it does not."""
    now = utcnow()
    try:
        with session_scope() as s:
            s.add(NotificationLog(dedupe_key=key, kind=kind, user_id=user_id,
                                  status=DeliveryStatus.SENDING, attempts=1))
        return None
    except IntegrityError:
        pass  # key exists: inspect it

    with session_scope() as s:
        row = s.scalar(select(NotificationLog).where(NotificationLog.dedupe_key == key))
        if row is None:  # deleted between statements; extremely unlikely
            return Outcome.IN_PROGRESS
        if row.status == DeliveryStatus.SENT:
            return Outcome.DUPLICATE
        if row.status == DeliveryStatus.FAILED:
            return Outcome.GAVE_UP
        lease_expired = row.updated_at <= now - timedelta(seconds=settings.delivery_lease_seconds)
        if row.status == DeliveryStatus.SENDING and not lease_expired:
            return Outcome.IN_PROGRESS
        if row.attempts >= settings.delivery_max_attempts:
            row.status = DeliveryStatus.FAILED
            return Outcome.GAVE_UP
        if row.status == DeliveryStatus.SENDING:
            logger.warning("Reclaiming stale delivery %s (previous attempt crashed; possible duplicate)", key)
        # Compare-and-swap: only one concurrent caller can win.
        won = s.execute(
            update(NotificationLog)
            .where(NotificationLog.id == row.id,
                   NotificationLog.status == row.status,
                   NotificationLog.updated_at == row.updated_at)
            .values(status=DeliveryStatus.SENDING, attempts=row.attempts + 1, updated_at=now)
        ).rowcount
        return None if won == 1 else Outcome.IN_PROGRESS


def _finish(key: str, *, message_id: int | None = None, error: str | None = None,
            settings: Settings) -> Outcome:
    with session_scope() as s:
        row = s.scalar(select(NotificationLog).where(NotificationLog.dedupe_key == key))
        assert row is not None
        if error is None:
            row.status = DeliveryStatus.SENT
            row.telegram_message_id = message_id
            row.last_error = None
            return Outcome.SENT
        row.last_error = error[:500]
        if row.attempts >= settings.delivery_max_attempts:
            row.status = DeliveryStatus.FAILED
            return Outcome.GAVE_UP
        row.status = DeliveryStatus.RETRY
        return Outcome.RETRY


async def deliver(key: str, kind: str, send: SendFn, settings: Settings,
                  user_id: int | None = None) -> Outcome:
    blocked = _claim(key, kind, user_id, settings)
    if blocked is not None:
        return blocked
    try:
        message_id = await send()
    except Exception as exc:  # noqa: BLE001 - every send failure is recorded
        err = redact(f"{type(exc).__name__}: {exc}", settings.secret_values())
        outcome = _finish(key, error=err, settings=settings)
        logger.warning("Delivery %s failed (%s): %s", key, outcome, err)
        return outcome
    return _finish(key, message_id=message_id, settings=settings)


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------
TextSender = Callable[[int, str], Awaitable[int | None]]  # (chat_id, html) -> message_id
LATE_THRESHOLD = timedelta(minutes=10)
DISPATCH_BATCH = 50


def recover_stale_reminders(settings: Settings) -> int:
    """Return reminders stuck in SENDING (crashed dispatcher) to PENDING."""
    cutoff = utcnow() - timedelta(seconds=settings.delivery_lease_seconds)
    with session_scope() as s:
        n = s.execute(
            update(Reminder)
            .where(Reminder.status == ReminderStatus.SENDING, Reminder.updated_at <= cutoff)
            .values(status=ReminderStatus.PENDING, updated_at=utcnow())
        ).rowcount or 0
    if n:
        logger.warning("Recovered %d reminder(s) stuck in SENDING", n)
    return n


def render_reminder(message: str, remind_at: datetime, now: datetime) -> str:
    text = f"⏰ <b>Reminder</b>\n{esc(message)}"
    if now - remind_at > LATE_THRESHOLD:
        text += f"\n<i>Delivered late; was due {esc(format_local(remind_at))}.</i>"
    return text


async def dispatch_due_reminders(send_text: TextSender, settings: Settings,
                                 on_sent: Callable[[int, int], None] | None = None) -> dict[str, int]:
    """Send due reminders. `on_sent(user_id, reminder_id)` fires once per new delivery."""
    recover_stale_reminders(settings)
    now = utcnow()
    with session_scope() as s:
        due = s.execute(
            select(Reminder.id, Reminder.message, Reminder.remind_at, Reminder.user_id,
                   User.telegram_chat_id, User.telegram_user_id)
            .join(User, User.id == Reminder.user_id)
            .where(Reminder.status == ReminderStatus.PENDING, Reminder.remind_at <= now,
                   User.is_active.is_(True))
            .order_by(Reminder.remind_at)
            .limit(DISPATCH_BATCH)
        ).all()

    stats: dict[str, int] = {}
    for rid, message, remind_at, user_id, chat_id, tg_user_id in due:
        with session_scope() as s:  # CAS claim on the reminder itself
            claimed = s.execute(
                update(Reminder)
                .where(Reminder.id == rid, Reminder.status == ReminderStatus.PENDING)
                .values(status=ReminderStatus.SENDING, updated_at=utcnow())
            ).rowcount
        if claimed != 1:
            continue
        target = chat_id or tg_user_id
        html = render_reminder(message, remind_at, now)
        outcome = await deliver(f"reminder:{rid}", "reminder",
                                lambda t=target, h=html: send_text(t, h), settings, user_id=user_id)
        with session_scope() as s:
            r = s.get(Reminder, rid)
            assert r is not None
            if outcome in (Outcome.SENT, Outcome.DUPLICATE):
                r.status = ReminderStatus.SENT
                r.sent_at = r.sent_at or utcnow()
                if outcome == Outcome.SENT and on_sent is not None:
                    try:
                        on_sent(user_id, rid)
                    except Exception:
                        logger.exception("on_sent callback failed for reminder %s", rid)
            elif outcome == Outcome.GAVE_UP:
                r.status = ReminderStatus.FAILED
            elif outcome == Outcome.RETRY:
                r.status = ReminderStatus.PENDING
            # IN_PROGRESS: another claim is live; leave SENDING for lease recovery
            if outcome in (Outcome.RETRY, Outcome.GAVE_UP):
                r.attempts += 1
                r.last_error = s.scalar(select(NotificationLog.last_error)
                                        .where(NotificationLog.dedupe_key == f"reminder:{rid}"))
        stats[outcome.value] = stats.get(outcome.value, 0) + 1
    return stats
