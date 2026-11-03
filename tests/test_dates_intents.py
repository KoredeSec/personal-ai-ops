from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.agents.chief_of_staff.dates import WhenError, extract_when, parse_when
from app.agents.chief_of_staff.intents import parse
from app.database.models import Priority

LAGOS = ZoneInfo("Africa/Lagos")
NOW = datetime(2026, 11, 3, 21, 0, tzinfo=LAGOS)  # Tuesday 21:00
MORNING = datetime(2026, 11, 3, 8, 0, tzinfo=LAGOS)  # Tuesday 08:00


def at(text, now=NOW):
    w = parse_when(text, now)
    return None if w is None else w.at.strftime("%a %d %b %Y %H:%M")


@pytest.mark.parametrize("text,expected", [
    ("tomorrow", "Wed 04 Nov 2026 09:00"),  # date only -> default time, not "now"
    ("tomorrow at 9am", "Wed 04 Nov 2026 09:00"),
    ("next monday", "Mon 09 Nov 2026 09:00"),
    ("monday 3pm", "Mon 09 Nov 2026 15:00"),
    ("on mon", "Mon 09 Nov 2026 09:00"),
    ("in 3 days", "Fri 06 Nov 2026 09:00"),
    ("in 2 hours", "Tue 03 Nov 2026 23:00"),
    ("in an hour", "Tue 03 Nov 2026 22:00"),
    ("friday", "Fri 06 Nov 2026 09:00"),
    ("next week", "Mon 09 Nov 2026 09:00"),
    ("next month", "Tue 01 Dec 2026 09:00"),
    ("this weekend", "Sat 07 Nov 2026 10:00"),
    ("15 nov", "Sun 15 Nov 2026 09:00"),
    ("nov 15 at 14:00", "Sun 15 Nov 2026 14:00"),
    ("20/11", "Fri 20 Nov 2026 09:00"),  # day/month order
    ("on the 20th", "Fri 20 Nov 2026 09:00"),
    ("2 jan", "Sat 02 Jan 2027 09:00"),  # passed this year -> next year
    ("8pm", "Wed 04 Nov 2026 20:00"),  # time already passed today -> tomorrow
    ("at 5", "Wed 04 Nov 2026 17:00"),  # bare small hour -> pm
    ("noon tomorrow", "Wed 04 Nov 2026 12:00"),
    ("midnight", "Wed 04 Nov 2026 00:00"),
    ("tonight", "Tue 03 Nov 2026 21:30"),  # said at 21:00: still tonight
    ("2026-11-20 14:00", "Fri 20 Nov 2026 14:00"),  # model-normalised form
])
def test_parse_when(text, expected):
    assert at(text) == expected


def test_this_weekday_today_when_time_ahead():
    assert at("this tuesday 6pm", MORNING) == "Tue 03 Nov 2026 18:00"
    assert at("tuesday", MORNING) == "Tue 10 Nov 2026 09:00"  # bare weekday = next one


def test_today_after_default_time_gives_next_slot():
    assert parse_when("today", datetime(2026, 11, 3, 10, 5, tzinfo=LAGOS)).at.strftime("%H:%M") == "10:30"


@pytest.mark.parametrize("text", ["this morning", "31/02", "13pm", "2025-01-01"])
def test_past_or_impossible_times_are_errors_not_guesses(text):
    with pytest.raises(WhenError):
        parse_when(text, NOW)


@pytest.mark.parametrize("text", ["asdf", "tue", "sometime", ""])
def test_unrecognised_returns_none(text):
    assert parse_when(text, NOW) is None


def test_default_time_preference_is_used():
    assert parse_when("friday", NOW, default_time=time(7, 30)).at.strftime("%H:%M") == "07:30"


def test_extract_keeps_surrounding_words():
    e = extract_when("call mum tomorrow at 6pm about the trip", NOW)
    assert e.remainder == "call mum about the trip"
    assert e.when.at.strftime("%d %H:%M") == "04 18:00"


def test_extract_marks_deadlines():
    e = extract_when("submit the Erasmus application by 15 Nov", NOW)
    assert e.when.is_deadline and e.remainder == "submit the Erasmus application"


def test_extract_ignores_lookalike_words():
    e = extract_when("I may need to call sat about the 5 tasks", NOW)
    assert e.when is None and e.remainder == "I may need to call sat about the 5 tasks"


# --------------------------------------------------------------------------
# Intents: the examples from the original brief, plus edge cases
# --------------------------------------------------------------------------
def test_brief_examples_route_without_ai():
    cases = {
        "Remind me to follow up with my mentor next Monday.": "add_reminder",
        "What are my outstanding commitments?": "list_tasks",
        "Find fully funded cybersecurity master's programmes in Europe.": "research",
        "Give me the latest developments in AWS security.": "research",
        "Prepare my priorities for tomorrow.": "plan_tomorrow",
        "Show me opportunities with deadlines within the next 30 days.": "research",
    }
    for text, action in cases.items():
        i = parse(text, NOW)
        assert i.action == action, text
        assert i.source == "rules"


def test_reminder_to_follow_up_tracks_the_person_too():
    i = parse("Remind me to follow up with my mentor next Monday.", NOW)
    assert i.text == "Follow up with my mentor" and i.person == "my mentor"
    assert i.when.at.strftime("%a %H:%M") == "Mon 09:00"


def test_task_with_deadline_and_priority_word():
    i = parse("add task submit Erasmus application by 15 Nov urgent", NOW)
    assert (i.action, i.text, i.priority) == ("add_task", "Submit Erasmus application", Priority.URGENT)
    assert i.when.is_deadline


def test_followup_person_and_context():
    i = parse("follow up with Dr Ade about the reference letter on friday", NOW)
    assert (i.action, i.person, i.context) == ("add_followup", "Dr Ade", "the reference letter")


@pytest.mark.parametrize("text,action", [
    ("done 12", "complete_task"), ("mark #7 as done", "complete_task"),
    ("remember that I prefer concise messages", "remember"),
    ("remember to buy airtime", "add_task"),  # "remember to" is a task, not a fact
    ("i need to renew my passport next month", "add_task"),
    ("what's overdue", "list_overdue"), ("brief me", "briefing_today"),
    ("weekly review", "weekly"), ("my follow-ups", "list_followups"),
    ("reminders", "list_reminders"), ("what do you know about me", "list_memory"),
    ("help", "help"), ("the cat sat on the mat", "unknown"),
])
def test_actions(text, action):
    assert parse(text, NOW).action == action


def test_recurring_is_flagged_not_guessed():
    assert parse("remind me every monday to file timesheet", NOW).recurring


def test_past_time_reported_as_error():
    i = parse("remind me to pay rent this morning", NOW)
    assert i.action == "add_reminder" and i.when is None and "past" in i.when_error
