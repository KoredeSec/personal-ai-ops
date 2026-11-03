"""End-to-end Telegram flows for the Chief of Staff over the fake transport."""

import time as _time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from telegram import Update

from app.agents.chief_of_staff import store
from app.bot.application import build_application
from app.core.llm import LLMResult
from app.database.models import AuditLog, DeliveryStatus, NotificationLog, User
from app.database.session import session_scope
from tests.test_bot import OWNER, FakeTelegram, StubLLM, make_update

_cb = 0


def make_callback(app, data, user_id=OWNER):
    global _cb
    _cb += 1
    return Update.de_json({"update_id": 10_000 + _cb, "callback_query": {
        "id": str(_cb), "chat_instance": "ci", "data": data,
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
        "message": {"message_id": 999, "date": int(_time.time()),
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": 1, "is_bot": True, "first_name": "Ops"}, "text": "x"}}}, app.bot)


class ModelLLM(StubLLM):
    payload = '{"action":"unknown"}'

    @property
    def configured(self):
        return True

    async def generate(self, prompt, **kw):
        return LLMResult(text=self.payload, model="stub", total_tokens=1)


@pytest.fixture()
async def bot(settings):
    fake = FakeTelegram()
    app = build_application(settings, request=fake, llm=ModelLLM(settings))
    await app.initialize()
    yield app, fake
    await app.shutdown()


def uid():
    with session_scope() as s:
        return s.scalar(select(User.id).where(User.telegram_user_id == OWNER))


def markup_of(fake, idx=-1):
    sends = [p for e, p in fake.calls if e == "sendMessage"]
    return sends[idx].get("reply_markup")


async def test_natural_language_reminder_then_undo_button(bot):
    app, fake = bot
    await app.process_update(make_update(app, "Remind me to follow up with my mentor next Monday"))
    assert "Reminder #1" in fake.sent_texts()[-1]
    markup = str(markup_of(fake))
    assert "cos:undo:rem:1" in markup and "cos:edit:rem:1" in markup
    await app.process_update(make_callback(app, "cos:undo:rem:1"))
    assert "Cancelled reminder #1" in fake.sent_texts()[-1]
    assert any(e == "editMessageReplyMarkup" for e, _ in fake.calls)  # single-use buttons removed
    assert store.pending_reminders(uid()) == [] and store.pending_followups(uid()) == []


async def test_edit_button_then_correction_message(bot):
    app, fake = bot
    await app.process_update(make_update(app, "add task pay rent by friday"))
    await app.process_update(make_callback(app, "cos:edit:task:1"))
    assert "Send the corrected version" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "pay rent and service charge monday 8am"))
    assert "pay rent and service charge" in fake.sent_texts()[-1]
    assert store.get_task(uid(), 1).title == "pay rent and service charge"


async def test_missing_time_question_answered_in_next_message(bot):
    app, fake = bot
    await app.process_update(make_update(app, "remind me to renew my passport"))
    assert "When should I remind you" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "next monday 9am"))
    assert "Renew my passport" in fake.sent_texts()[-1]
    assert len(store.pending_reminders(uid())) == 1


async def test_cancel_clears_pending_question(bot):
    app, fake = bot
    await app.process_update(make_update(app, "remind me to renew my passport"))
    await app.process_update(make_update(app, "/cancel"))
    await app.process_update(make_update(app, "next monday 9am"))
    assert store.pending_reminders(uid()) == []


async def test_unknown_text_goes_to_model_and_is_labelled(bot):
    app, fake = bot
    app.bot_data["llm"].payload = '{"action":"add_task","text":"Sort visa paperwork"}'
    await app.process_update(make_update(app, "ugh the visa paperwork needs sorting at some point"))
    reply = fake.sent_texts()[-1]
    assert "Sort visa paperwork" in reply and "interpreted by AI" in reply


async def test_model_unknown_gives_examples(bot):
    app, fake = bot
    await app.process_update(make_update(app, "the cat sat on the mat"))
    assert "not sure what you'd like" in fake.sent_texts()[-1]


async def test_commands(bot):
    app, fake = bot
    await app.process_update(make_update(app, "/addtask renew passport by 20 Nov urgent"))
    assert "Renew passport" in fake.sent_texts()[-1] and "urgent" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "/tasks"))
    assert "Upcoming (1)" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "/done 1"))
    assert "Done: #1" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "/remember I prefer concise replies"))
    await app.process_update(make_update(app, "/memory"))
    assert "I prefer concise replies" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "/forget all"))
    assert "can't be undone" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "/settings quiet 23:00-06:30"))
    assert "quiet_hours</b> = 23:00-06:30" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "/settings quiet 99:00"))
    assert "should look like 22:00-07:00" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "/today"))
    assert "Today ·" in fake.sent_texts()[-1]
    await app.process_update(make_update(app, "/research cloud security"))
    assert "Research Agent isn't available yet" in fake.sent_texts()[-1]


async def test_export_sends_a_json_document(bot):
    app, fake = bot
    await app.process_update(make_update(app, "/addtask keep me"))
    await app.process_update(make_update(app, "/export"))
    assert any(e == "sendDocument" for e, _ in fake.calls)


async def test_forged_callback_data_is_ignored(bot):
    app, fake = bot
    before = len(fake.sent_texts())
    for data in ("cos:undo:task:abc", "evil", "cos:drop:table:1", "cos:done:task:999"):
        await app.process_update(make_callback(app, data))
    texts = fake.sent_texts()[before:]
    assert all(t in ("That item no longer exists.", "That button has expired.") for t in texts)


async def test_stranger_cannot_press_buttons(bot):
    app, fake = bot
    await app.process_update(make_update(app, "/addtask mine"))
    before = len(fake.calls)
    await app.process_update(make_callback(app, "cos:done:task:1", user_id=666))
    assert len(fake.calls) == before  # silently dropped by the auth gate


# --------------------------------------------------------------------------
# Scheduled delivery
# --------------------------------------------------------------------------
class Ctx:
    def __init__(self, app):
        self.application = app


async def test_planner_sends_briefing_once_per_day(bot, monkeypatch):
    from app.bot import jobs

    app, fake = bot
    await app.process_update(make_update(app, "/start"))
    await app.process_update(make_update(app, "/addtask prep slides by tomorrow 3pm"))
    # 07:30 Lagos on a Wednesday: inside the briefing window, not the weekly one
    fixed = datetime(2026, 11, 4, 6, 30, tzinfo=UTC)
    monkeypatch.setattr(jobs, "now_local", lambda: fixed.astimezone(app.bot_data["settings"].tz))
    monkeypatch.setattr(jobs, "utcnow", lambda: fixed)
    before = len(fake.sent_texts())
    await jobs.planner_job(Ctx(app))
    await jobs.planner_job(Ctx(app))  # second run in the same window: no duplicate
    sent = fake.sent_texts()[before:]
    assert len(sent) == 1 and "Today · Wed 04 Nov" in sent[0]
    with session_scope() as s:
        row = s.scalar(select(NotificationLog).where(NotificationLog.kind == "briefing"))
        assert row.status == DeliveryStatus.SENT and row.dedupe_key.endswith("2026-11-04")


async def test_planner_sends_weekly_review_on_configured_day(bot, monkeypatch):
    from app.bot import jobs

    app, fake = bot
    await app.process_update(make_update(app, "/start"))
    sunday_1830 = datetime(2026, 11, 8, 17, 30, tzinfo=UTC)
    monkeypatch.setattr(jobs, "now_local", lambda: sunday_1830.astimezone(app.bot_data["settings"].tz))
    monkeypatch.setattr(jobs, "utcnow", lambda: sunday_1830)
    before = len(fake.sent_texts())
    await jobs.planner_job(Ctx(app))
    sent = fake.sent_texts()[before:]
    assert any("Week review" in t for t in sent)


async def test_job_alert_held_during_quiet_hours_and_shown_in_briefing(bot, monkeypatch):
    from app.bot import jobs

    app, fake = bot
    await app.process_update(make_update(app, "/start"))
    three_am = datetime(2026, 11, 4, 2, 0, tzinfo=UTC)
    monkeypatch.setattr(jobs, "now_local", lambda: three_am.astimezone(app.bot_data["settings"].tz))
    before = len(fake.sent_texts())
    await jobs.on_job_error("backup", RuntimeError("disk full"), Ctx(app))
    assert fake.sent_texts()[before:] == []  # quiet hours: nothing sent
    with session_scope() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.event_type == "job_failed"))
    await app.process_update(make_update(app, "/today"))
    assert "backup failed" in fake.sent_texts()[-1]


async def test_user_reminders_fire_even_in_quiet_hours(bot):
    from app.bot import jobs

    app, fake = bot
    await app.process_update(make_update(app, "/start"))
    store.create_reminder(uid(), "Take medication", datetime.now(UTC) + timedelta(seconds=1))
    with session_scope() as s:
        from sqlalchemy import update

        from app.database.models import Reminder
        s.execute(update(Reminder).values(remind_at=datetime.now(UTC) - timedelta(seconds=1)))
    before = len(fake.sent_texts())
    await jobs.reminder_dispatch_job(Ctx(app))
    assert "Take medication" in fake.sent_texts()[before:][0]
