import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.timeutils import utcnow
from app.database.models import (
    DeliveryStatus,
    NotificationLog,
    Reminder,
    ReminderStatus,
    User,
)
from app.database.session import session_scope
from app.services.notifications import (
    Outcome,
    deliver,
    dispatch_due_reminders,
    recover_stale_reminders,
    render_reminder,
)


class Sender:
    def __init__(self, fail_times=0):
        self.sent: list[tuple[int, str]] = []
        self.fail_times = fail_times

    async def __call__(self, chat_id, html):
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("telegram down")
        self.sent.append((chat_id, html))
        return len(self.sent)


def once(sender, chat=1, text="hi"):
    return lambda: sender(chat, text)


def row(key):
    with session_scope() as s:
        r = s.scalar(select(NotificationLog).where(NotificationLog.dedupe_key == key))
        s.expunge(r)
        return r


async def test_deliver_once_then_duplicate(settings):
    snd = Sender()
    assert await deliver("k", "test", once(snd), settings) == Outcome.SENT
    assert await deliver("k", "test", once(snd), settings) == Outcome.DUPLICATE
    assert len(snd.sent) == 1
    r = row("k")
    assert r.status == DeliveryStatus.SENT and r.telegram_message_id == 1 and r.attempts == 1


async def test_failure_then_retry_succeeds(settings):
    snd = Sender(fail_times=1)
    assert await deliver("k", "test", once(snd), settings) == Outcome.RETRY
    assert row("k").status == DeliveryStatus.RETRY and "telegram down" in row("k").last_error
    assert await deliver("k", "test", once(snd), settings) == Outcome.SENT
    assert row("k").attempts == 2 and len(snd.sent) == 1


async def test_gives_up_after_max_attempts(settings):
    settings.delivery_max_attempts = 2
    snd = Sender(fail_times=10)
    assert await deliver("k", "t", once(snd), settings) == Outcome.RETRY
    assert await deliver("k", "t", once(snd), settings) == Outcome.GAVE_UP
    assert await deliver("k", "t", once(snd), settings) == Outcome.GAVE_UP
    assert row("k").status == DeliveryStatus.FAILED and snd.sent == []


async def test_live_claim_blocks_concurrent_send(settings):
    gate = asyncio.Event()
    sent = []

    async def slow():
        await gate.wait()
        sent.append(1)
        return 1

    first = asyncio.create_task(deliver("k", "t", slow, settings))
    await asyncio.sleep(0.01)
    second = await deliver("k", "t", slow, settings)
    gate.set()
    assert second == Outcome.IN_PROGRESS
    assert await first == Outcome.SENT and sent == [1]


def _crash_leaves_sending(key, age_seconds):
    with session_scope() as s:
        s.add(NotificationLog(dedupe_key=key, kind="t", status=DeliveryStatus.SENDING, attempts=1))
    with session_scope() as s:
        s.execute(update(NotificationLog).where(NotificationLog.dedupe_key == key)
                  .values(updated_at=utcnow() - timedelta(seconds=age_seconds)))


async def test_crashed_claim_within_lease_is_not_resent(settings):
    _crash_leaves_sending("k", age_seconds=10)
    snd = Sender()
    assert await deliver("k", "t", once(snd), settings) == Outcome.IN_PROGRESS
    assert snd.sent == []


async def test_crashed_claim_after_lease_is_reclaimed(settings):
    _crash_leaves_sending("k", age_seconds=settings.delivery_lease_seconds + 5)
    snd = Sender()
    assert await deliver("k", "t", once(snd), settings) == Outcome.SENT
    assert len(snd.sent) == 1 and row("k").attempts == 2


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------
def _user(active=True):
    with session_scope() as s:
        u = User(telegram_user_id=1001, telegram_chat_id=5005, is_active=active)
        s.add(u)
        s.flush()
        return u.id


def _reminder(user_id, minutes_from_now, message="Call mentor"):
    with session_scope() as s:
        r = Reminder(user_id=user_id, message=message, remind_at=utcnow() + timedelta(minutes=minutes_from_now))
        s.add(r)
        s.flush()
        return r.id


def _get(rid):
    with session_scope() as s:
        r = s.get(Reminder, rid)
        s.expunge(r)
        return r


async def test_due_reminder_sent_once_future_untouched(settings):
    uid = _user()
    due, future = _reminder(uid, -1), _reminder(uid, 60)
    snd = Sender()
    assert await dispatch_due_reminders(snd, settings) == {"sent": 1}
    assert await dispatch_due_reminders(snd, settings) == {}
    assert len(snd.sent) == 1 and snd.sent[0][0] == 5005
    assert "Call mentor" in snd.sent[0][1]
    assert _get(due).status == ReminderStatus.SENT and _get(due).sent_at is not None
    assert _get(future).status == ReminderStatus.PENDING


async def test_reminder_text_is_escaped(settings):
    uid = _user()
    _reminder(uid, -1, message="<script>x</script> & co")
    snd = Sender()
    await dispatch_due_reminders(snd, settings)
    assert "&lt;script&gt;" in snd.sent[0][1] and "<script>" not in snd.sent[0][1]


async def test_failed_send_returns_to_pending_with_error(settings):
    uid = _user()
    rid = _reminder(uid, -1)
    snd = Sender(fail_times=1)
    assert await dispatch_due_reminders(snd, settings) == {"retry": 1}
    r = _get(rid)
    assert r.status == ReminderStatus.PENDING and r.attempts == 1 and "telegram down" in r.last_error
    assert await dispatch_due_reminders(snd, settings) == {"sent": 1}


async def test_permanent_failure_marks_reminder_failed(settings):
    settings.delivery_max_attempts = 2
    uid = _user()
    rid = _reminder(uid, -1)
    snd = Sender(fail_times=10)
    await dispatch_due_reminders(snd, settings)
    await dispatch_due_reminders(snd, settings)
    assert _get(rid).status == ReminderStatus.FAILED


async def test_crash_after_send_before_reminder_marked_does_not_resend(settings):
    """Ledger says SENT, reminder stuck in SENDING: recovery must not double-send."""
    uid = _user()
    rid = _reminder(uid, -1)
    snd = Sender()
    assert await deliver(f"reminder:{rid}", "reminder", once(snd), settings) == Outcome.SENT
    stale = utcnow() - timedelta(seconds=settings.delivery_lease_seconds + 5)
    with session_scope() as s:
        s.execute(update(Reminder).where(Reminder.id == rid)
                  .values(status=ReminderStatus.SENDING, updated_at=stale))
    assert recover_stale_reminders(settings) == 1
    assert await dispatch_due_reminders(snd, settings) == {"duplicate": 1}
    assert len(snd.sent) == 1 and _get(rid).status == ReminderStatus.SENT


async def test_inactive_user_reminders_skipped(settings):
    uid = _user(active=False)
    _reminder(uid, -1)
    snd = Sender()
    assert await dispatch_due_reminders(snd, settings) == {}


def test_late_reminder_says_when_it_was_due():
    now = utcnow()
    assert "Delivered late" in render_reminder("x", now - timedelta(hours=2), now)
    assert "Delivered late" not in render_reminder("x", now - timedelta(minutes=1), now)


async def test_concurrent_dispatchers_send_once(settings):
    uid = _user()
    _reminder(uid, -1)
    snd = Sender()
    await asyncio.gather(dispatch_due_reminders(snd, settings), dispatch_due_reminders(snd, settings))
    assert len(snd.sent) == 1


@pytest.fixture(autouse=True)
def _db(db):
    yield
