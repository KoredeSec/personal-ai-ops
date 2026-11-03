"""Chief of Staff behaviour: store, planning, decisions, agent actions, preferences."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.agents.chief_of_staff import agent, decisions, planning, store
from app.agents.chief_of_staff.intents import parse
from app.agents.chief_of_staff.preferences import PreferenceError, get_prefs, set_pref, validate
from app.core.llm import GeminiClient, LLMResult
from app.database.models import (
    DecisionEvent,
    DecisionOutcome,
    MemoryCategory,
    Priority,
    TaskStatus,
    User,
)
from app.database.session import session_scope

# Tue 03 Nov 2026 20:00 UTC = 21:00 Lagos
NOW = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)


@pytest.fixture()
def uid(db):
    with session_scope() as s:
        u = User(telegram_user_id=1001, telegram_chat_id=1001)
        s.add(u)
        s.flush()
        return u.id


def ctx(uid, llm=None, now=NOW):
    return agent.Ctx(user_id=uid, prefs=get_prefs(uid), llm=llm, now=now)


async def run(uid, text, llm=None, now=NOW):
    c = ctx(uid, llm, now)
    return await agent.execute(parse(text, c.local_now, c.prefs.default_time), c)


def events(uid):
    with session_scope() as s:
        return [(e.kind, e.outcome) for e in s.scalars(select(DecisionEvent).where(DecisionEvent.user_id == uid))]


# --------------------------------------------------------------------------
# Create via natural language
# --------------------------------------------------------------------------
async def test_reminder_with_followup_creates_both_and_logs_decision(uid):
    r = await run(uid, "Remind me to follow up with my mentor next Monday")
    assert "Reminder #1" in r.html and "Mon 09 Nov 2026, 09:00 WAT" in r.html
    assert "follow-up with <b>my mentor</b>" in r.html
    rem = store.pending_reminders(uid)[0]
    fu = store.pending_followups(uid)[0]
    assert rem.followup_id == fu.id and fu.next_follow_up_at == rem.remind_at
    assert [b[1] for b in r.buttons[0]] == ["cos:undo:rem:1", "cos:edit:rem:1"]
    assert events(uid) == [("request_handled", DecisionOutcome.REMOVED)]


async def test_reminder_without_time_asks_and_completes_on_answer(uid):
    r = await run(uid, "remind me to call the bank")
    assert r.pending == {"op": "need_time", "intent": "add_reminder", "text": "Call the bank",
                         "person": None, "context": None}
    assert store.pending_reminders(uid) == []
    done = await agent.complete_pending_time(ctx(uid), r.pending, "tomorrow 10am")
    assert "Call the bank" in done.html and "Wed 04 Nov 2026, 10:00 WAT" in done.html
    assert ("clarification_asked", DecisionOutcome.ADDED) in events(uid)


async def test_pending_time_reasks_on_garbage(uid):
    r = await run(uid, "remind me to call the bank")
    again = await agent.complete_pending_time(ctx(uid), r.pending, "whenever really")
    assert again.pending == r.pending and "couldn't read a time" in again.html


async def test_recurring_reminder_is_refused_honestly(uid):
    r = await run(uid, "remind me every monday to file timesheet")
    assert "aren't supported yet" in r.html and store.pending_reminders(uid) == []


async def test_past_time_is_rejected_not_guessed(uid):
    r = await run(uid, "remind me to pay rent this morning")
    assert "in the past" in r.html and store.pending_reminders(uid) == []


async def test_task_with_deadline_priority_and_buttons(uid):
    r = await run(uid, "add task submit Erasmus application by 15 Nov urgent")
    t = store.open_tasks(uid)[0]
    assert (t.title, t.priority) == ("Submit Erasmus application", Priority.URGENT)
    assert t.due_at == datetime(2026, 11, 15, 8, 0, tzinfo=UTC)  # 09:00 WAT
    labels = [b[0] for row in r.buttons for b in row]
    assert labels == ["↩️ Undo", "✏️ Edit", "✅ Done", "⏰ Remind me"]


async def test_followup_never_claims_to_contact_anyone(uid):
    r = await run(uid, "follow up with Dr Ade about the reference letter on friday")
    assert "I never contact anyone myself" in r.html
    f = store.pending_followups(uid)[0]
    assert (f.person_name, f.context) == ("Dr Ade", "the reference letter")


async def test_remember_guesses_category_and_offers_correction(uid):
    r = await run(uid, "remember that my long-term goal is a cloud security role in Europe")
    m = store.list_memory(uid)[0]
    assert m.category == MemoryCategory.OBJECTIVE
    assert any(b[1] == f"cos:cat:mem:{m.id}:constraint" for row in r.buttons for b in row)
    fixed = agent.button_action(ctx(uid), "cat", "mem", m.id, "constraint")
    assert "constraint" in fixed.html and store.list_memory(uid)[0].category == MemoryCategory.CONSTRAINT


async def test_html_in_user_text_is_escaped(uid):
    r = await run(uid, "add task <b>fix</b> & <script>")
    assert "&lt;b&gt;fix&lt;/b&gt; &amp; &lt;script&gt;" in r.html


# --------------------------------------------------------------------------
# Undo / edit / buttons
# --------------------------------------------------------------------------
async def test_undo_reminder_cancels_linked_followup_and_counts_as_fix(uid):
    await run(uid, "Remind me to follow up with my mentor next Monday")
    agent.button_action(ctx(uid), "undo", "rem", 1)
    assert store.pending_reminders(uid) == [] and store.pending_followups(uid) == []
    assert ("interpretation_fixed", DecisionOutcome.ADDED) in events(uid)


async def test_edit_reminder_changes_text_and_time(uid):
    await run(uid, "remind me to call mentor next monday")
    r = agent.apply_edit(ctx(uid), "rem", 1, "call mentor tuesday 10am")
    rem = store.pending_reminders(uid)[0]
    assert rem.message == "call mentor" and rem.remind_at == datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    assert "Tue 10 Nov 2026, 10:00 WAT" in r.html


async def test_edit_task_only_time(uid):
    await run(uid, "add task pay rent by friday")
    agent.apply_edit(ctx(uid), "task", 1, "monday 8am")
    t = store.get_task(uid, 1)
    assert t.title == "Pay rent" and t.due_at.hour == 7  # 08:00 WAT


async def test_done_and_reopen_buttons(uid):
    await run(uid, "add task pay rent")
    agent.button_action(ctx(uid), "done", "task", 1)
    assert store.get_task(uid, 1).status == TaskStatus.DONE
    agent.button_action(ctx(uid), "reopen", "task", 1)
    assert store.get_task(uid, 1).status == TaskStatus.OPEN


async def test_completing_task_cancels_its_reminder(uid):
    await run(uid, "add task submit form by friday 5pm")
    agent.button_action(ctx(uid), "remindat", "task", 1)
    assert len(store.pending_reminders(uid)) == 1
    agent.button_action(ctx(uid), "done", "task", 1)
    assert store.pending_reminders(uid) == []


async def test_buttons_cannot_touch_another_users_items(uid):
    with session_scope() as s:
        other = User(telegram_user_id=2002)
        s.add(other)
        s.flush()
        other_id = other.id
    await run(uid, "add task mine")
    r = agent.button_action(ctx(other_id), "done", "task", 1)
    assert r.html == "That item no longer exists."
    assert store.get_task(uid, 1).status == TaskStatus.OPEN


async def test_contacted_closes_followup(uid):
    await run(uid, "follow up with Ade")
    agent.button_action(ctx(uid), "contacted", "fu", 1)
    with pytest.raises(IndexError):
        store.pending_followups(uid)[0]


# --------------------------------------------------------------------------
# Lists
# --------------------------------------------------------------------------
async def test_task_list_groups_and_buttons(uid):
    store.create_task(uid, "Late thing", due_at=NOW - timedelta(days=2))
    store.create_task(uid, "Future thing", due_at=NOW + timedelta(days=2))
    store.create_task(uid, "Someday thing")
    r = await run(uid, "what are my outstanding commitments?")
    assert r.html.index("Overdue (1)") < r.html.index("Upcoming (1)") < r.html.index("No date (1)")
    assert [b[1] for b in r.buttons[0]] == ["cos:done:task:1", "cos:done:task:2", "cos:done:task:3"]


async def test_neglected_followup_flagged(uid):
    f = store.create_followup(uid, "Old contact")
    with session_scope() as s:
        from app.database.models import FollowUp
        s.get(FollowUp, f.id).created_at = NOW - timedelta(days=20)
    r = await run(uid, "my follow-ups")
    assert "neglected" in r.html


async def test_forget_all_requires_explicit_button(uid):
    store.add_memory(uid, "fact one")
    store.add_memory(uid, "fact two")
    r = agent.button_action(ctx(uid), "forgetall", "mem", 0)
    assert "Deleted all 2" in r.html and store.list_memory(uid) == []


async def test_export_contains_data_and_no_secrets(uid):
    await run(uid, "add task export me")
    await run(uid, "remember that I study at night")
    blob = agent.export_json(uid).decode()
    assert "Export me" in blob and "I study at night" in blob
    assert "AIza" not in blob and "TEST_TOKEN" not in blob


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def test_ranking_overdue_beats_priority():
    from app.database.models import Task
    a = Task(id=1, title="a", priority=Priority.URGENT, due_at=NOW + timedelta(days=10))
    b = Task(id=2, title="b", priority=Priority.LOW, due_at=NOW - timedelta(hours=3))
    c = Task(id=3, title="c", priority=Priority.MEDIUM, due_at=NOW + timedelta(hours=5))
    assert [t.id for t in planning.rank([a, b, c], NOW)] == [2, 3, 1]


def test_overlap_detection(uid):
    s1 = NOW + timedelta(hours=13)
    store.create_task(uid, "Meeting", scheduled_start=s1, scheduled_end=s1 + timedelta(hours=1))
    store.create_task(uid, "Call", scheduled_start=s1 + timedelta(minutes=30), scheduled_end=s1 + timedelta(hours=2))
    store.create_task(uid, "Later", scheduled_start=s1 + timedelta(hours=3), scheduled_end=s1 + timedelta(hours=4))
    conflicts = planning.find_overlaps(store.open_tasks(uid))
    assert [c.task_ids for c in conflicts] == [(1, 2)]


def test_free_time_excludes_work_and_quiet_hours(uid):
    prefs = get_prefs(uid)  # quiet 22:00-07:00, work mon-fri 09:00-17:00
    wed = date(2026, 11, 4)
    ft = planning.free_time(wed, prefs, [], NOW)
    # 07:00-22:00 = 15h minus 8h work = 7h
    assert ft.minutes == 7 * 60
    sat = planning.free_time(date(2026, 11, 7), prefs, [], NOW)
    assert sat.minutes == 15 * 60  # no work on Saturday


def test_free_time_subtracts_task_effort(uid):
    store.create_task(uid, "Essay", due_at=datetime(2026, 11, 4, 16, 0, tzinfo=UTC), estimated_minutes=90)
    store.create_task(uid, "Quick", due_at=datetime(2026, 11, 4, 17, 0, tzinfo=UTC))
    b = planning.build_briefing(uid, get_prefs(uid), date(2026, 11, 4), NOW)
    assert b.free.minutes == 7 * 60 - 90 - 30
    assert any("30 min each" in a for a in b.free.assumptions)


def test_briefing_sections_and_decisions(uid):
    store.create_task(uid, "Overdue report", due_at=NOW - timedelta(days=1), priority=Priority.HIGH)
    store.create_task(uid, "Due tomorrow", due_at=datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    store.create_followup(uid, "Ade", next_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC))
    r = agent.briefing_reply(ctx(uid), date(2026, 11, 4))
    html = r.html
    for section in ("Top priorities", "Overdue (1)", "Follow-ups", "Time for personal development"):
        assert section in html, section
    kinds = events(uid)
    assert ("briefing_ranked", DecisionOutcome.REMOVED) in kinds
    assert ("followup_surfaced", DecisionOutcome.REMOVED) in kinds
    assert ("overdue_flagged", DecisionOutcome.ADDED) in kinds


def test_empty_briefing_is_short_and_honest(uid):
    html = agent.briefing_reply(ctx(uid), date(2026, 11, 4)).html
    assert "Nothing due, overdue or scheduled." in html and "Top priorities" not in html


class StubLLM(GeminiClient):
    def __init__(self, settings, payload=None, fail=False):
        super().__init__(settings)
        self.payload, self.fail, self.calls = payload, fail, 0

    @property
    def configured(self):
        return True

    async def generate(self, prompt, **kw):
        self.calls += 1
        if self.fail:
            from app.core.llm import LLMQuotaExceededError
            raise LLMQuotaExceededError("quota")
        return LLMResult(text=self.payload, model="stub")


async def test_weekly_review_uses_ai_priorities_and_reports_decisions(uid, settings):
    store.create_task(uid, "Finish AWS lab", due_at=NOW + timedelta(days=3))
    t = store.create_task(uid, "Done thing")
    store.complete_task(uid, t.id)
    store.add_memory(uid, "My goal is a cloud security role", MemoryCategory.OBJECTIVE)
    decisions.removed(uid, "reminder_delivered")
    llm = StubLLM(settings, '{"priorities":[{"title":"Finish AWS lab","reason":"Advances the cloud security goal"}]}')
    # review "now" just after the real clock, which stamped the rows above
    r = await agent.weekly_reply(ctx(uid, llm, now=datetime.now(UTC) + timedelta(minutes=1)))
    assert "Advances the cloud security goal" in r.html and "(by deadline" not in r.html
    assert "Taken off your plate: 1" in r.html
    assert "Open loops: 2 opened, 1 closed (net +1)" in r.html
    assert "created more work than it absorbed" in r.html
    assert "My goal is a cloud security role" in r.html


async def test_weekly_review_falls_back_without_ai(uid, settings):
    store.create_task(uid, "Finish AWS lab", due_at=NOW + timedelta(days=3))
    r = await agent.weekly_reply(ctx(uid, StubLLM(settings, fail=True)))
    assert "(by deadline and priority)" in r.html and "Finish AWS lab" in r.html


# --------------------------------------------------------------------------
# NLU fallback
# --------------------------------------------------------------------------
async def test_model_fallback_dates_are_reparsed_and_past_rejected(uid, settings):
    from app.agents.chief_of_staff.nlu import classify

    good = StubLLM(settings, '{"action":"add_task","text":"Book flights","when":"2026-11-10 12:00"}')
    i = await classify(good, "sort out flights for the trip around lunch next tuesday", ctx(uid).local_now)
    assert (i.action, i.text, i.source) == ("add_task", "Book flights", "model")
    assert i.when.at.strftime("%a %H:%M") == "Tue 12:00"
    bad = StubLLM(settings, '{"action":"add_task","text":"Time travel","when":"2020-01-01 09:00"}')
    i2 = await classify(bad, "do it in 2020", ctx(uid).local_now)
    assert i2.when is None and "past" in i2.when_error


# --------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------
@pytest.mark.parametrize("key,value,expected", [
    ("briefing_time", "6:45", "06:45"), ("weekly_review", "Sunday 18:00", "sun 18:00"),
    ("quiet_hours", "23:00-06:30", "23:00-06:30"), ("quiet_hours", "off", "off"),
    ("work_hours", "mon-fri 08:30-17:30", "mon-fri 08:30-17:30"),
])
def test_valid_preferences(key, value, expected):
    assert validate(key, value) == expected


@pytest.mark.parametrize("key,value", [
    ("briefing_time", "25:00"), ("weekly_review", "someday 18:00"), ("quiet_hours", "22:00"),
    ("work_hours", "mon-fri 17:00-09:00"), ("nonsense", "x"),
])
def test_invalid_preferences(key, value):
    with pytest.raises(PreferenceError):
        validate(key, value)


def test_quiet_hours_wrap_midnight(uid):
    from zoneinfo import ZoneInfo
    p = get_prefs(uid)
    tz = ZoneInfo("Africa/Lagos")
    assert p.in_quiet_hours(datetime(2026, 11, 3, 23, 0, tzinfo=tz))
    assert p.in_quiet_hours(datetime(2026, 11, 4, 6, 59, tzinfo=tz))
    assert not p.in_quiet_hours(datetime(2026, 11, 4, 7, 0, tzinfo=tz))
    set_pref(uid, "quiet_hours", "off")
    assert not get_prefs(uid).in_quiet_hours(datetime(2026, 11, 3, 23, 0, tzinfo=tz))


def test_default_time_preference_flows_into_parsing(uid):
    set_pref(uid, "default_time", "07:30")
    c = ctx(uid)
    i = parse("add task gym friday", c.local_now, c.prefs.default_time)
    assert i.when.at.time() == time(7, 30)
