"""Model fallback for messages the rules don't recognise.

The model only *classifies and extracts*. It picks one action from the
closed ACTIONS set and copies phrases from the message; it never executes
anything, and the date it returns is re-parsed by our own deterministic
parser (an invalid or past date is rejected, not trusted).
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.chief_of_staff.dates import WhenError, parse_when
from app.agents.chief_of_staff.intents import Intent
from app.core.llm import GeminiClient
from app.core.security import sanitize_user_text
from app.database.models import Priority

Action = Literal[
    "add_task", "add_reminder", "add_followup", "remember", "complete_task",
    "list_tasks", "list_overdue", "briefing_today", "plan_tomorrow", "weekly",
    "list_followups", "list_reminders", "list_memory", "research", "help", "unknown",
]


class NLUResult(BaseModel):
    action: Action
    text: str | None = Field(default=None, description="Task title, reminder message or fact to remember, without the date phrase")
    when: str | None = Field(default=None, description="Date/time as 'YYYY-MM-DD HH:MM' in local time, or null")
    person: str | None = None
    context: str | None = None
    task_id: int | None = None
    priority: Literal["low", "medium", "high", "urgent"] | None = None


SYSTEM = """You classify one message sent by the owner of a personal assistant.
Choose exactly one action. Copy wording from the message; do not invent details.
Actions:
- add_task: something the owner must do (text = the task)
- add_reminder: the owner wants to be reminded at a time (text = what to remind)
- add_followup: the owner needs to contact a person (person, context)
- remember: a durable fact about the owner (goals, constraints, preferences)
- complete_task: the owner finished task number N (task_id)
- list_tasks, list_overdue, list_followups, list_reminders, list_memory: show stored items
- briefing_today, plan_tomorrow, weekly: planning summaries
- research: finding opportunities, scholarships, programmes or security news
- help: asking what the assistant can do
- unknown: anything else, or if unsure
For 'when', convert relative dates using the current local time given. If no date is stated, use null.
Never use the past. Never perform the request; only classify it."""


async def classify(llm: GeminiClient, text: str, now: datetime, default_time: time = time(9), *,
                   user_id: int | None = None, deadline_seconds: float | None = None) -> Intent:
    clean = sanitize_user_text(text, 1000)
    prompt = (f"Current local time: {now:%A %Y-%m-%d %H:%M} ({now.tzname()}).\n"
              f"Message: {clean}")
    r = await llm.generate_json(prompt, NLUResult, system_instruction=SYSTEM, tier="light",
                                temperature=0, max_output_tokens=512, agent="chief_of_staff",
                                action="nlu", user_id=user_id, deadline_seconds=deadline_seconds)
    when = when_error = None
    if r.when:
        try:
            when = parse_when(r.when, now, default_time)
            if when is None:
                when_error = f"I couldn't use the time '{r.when}'"
        except WhenError as exc:
            when_error = str(exc)
    return Intent(
        action=r.action,
        text=(r.text or "").strip()[:500] or None,
        when=when,
        when_text=r.when if when is None else None,
        when_error=when_error,
        person=(r.person or "").strip()[:200] or None,
        context=(r.context or "").strip()[:500] or None,
        task_id=r.task_id,
        priority=Priority(r.priority) if r.priority else None,
        source="model",
    )
