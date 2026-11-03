"""Deterministic data operations for the Chief of Staff.

Every function takes the internal `user_id` and filters by it, so an item ID
from a button or command can never touch another user's data. Objects are
returned detached (safe to read after the session closes).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.timeutils import utcnow
from app.database.models import (
    FollowUp,
    FollowUpStatus,
    MemoryCategory,
    MemoryItem,
    Priority,
    Reminder,
    ReminderStatus,
    Task,
    TaskStatus,
)
from app.database.session import session_scope
from app.services.audit import AuditEvent, audit


class NotFound(LookupError):
    pass


MAX_TITLE = 500


def _detach(s, obj):
    s.flush()
    s.expunge(obj)
    return obj


def _get(s, model, item_id: int, user_id: int):
    obj = s.get(model, item_id)
    if obj is None or obj.user_id != user_id:
        raise NotFound(f"No {model.__name__.lower()} #{item_id}")
    return obj


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------
def create_task(user_id: int, title: str, *, due_at: datetime | None = None,
                priority: Priority = Priority.MEDIUM, category: str | None = None,
                scheduled_start: datetime | None = None, scheduled_end: datetime | None = None,
                estimated_minutes: int | None = None) -> Task:
    title = title.strip()[:MAX_TITLE]
    if not title:
        raise ValueError("A task needs a title")
    if scheduled_start and scheduled_end and scheduled_end <= scheduled_start:
        raise ValueError("A time block must end after it starts")
    with session_scope() as s:
        t = Task(user_id=user_id, title=title, due_at=due_at, priority=priority, category=category,
                 scheduled_start=scheduled_start, scheduled_end=scheduled_end,
                 estimated_minutes=estimated_minutes)
        s.add(t)
        return _detach(s, t)


def get_task(user_id: int, task_id: int) -> Task:
    with session_scope() as s:
        return _detach(s, _get(s, Task, task_id, user_id))


def update_task(user_id: int, task_id: int, **fields) -> Task:
    allowed = {"title", "due_at", "priority", "category", "scheduled_start", "scheduled_end",
               "estimated_minutes"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Can't update {', '.join(sorted(bad))}")
    with session_scope() as s:
        t = _get(s, Task, task_id, user_id)
        for k, v in fields.items():
            setattr(t, k, v.strip()[:MAX_TITLE] if k == "title" else v)
        return _detach(s, t)


def complete_task(user_id: int, task_id: int) -> Task:
    with session_scope() as s:
        t = _get(s, Task, task_id, user_id)
        if t.status != TaskStatus.DONE:
            t.status = TaskStatus.DONE
            t.completed_at = utcnow()
        # completing a task cancels its pending reminders
        for r in s.scalars(select(Reminder).where(Reminder.task_id == t.id,
                                                  Reminder.status == ReminderStatus.PENDING)):
            r.status = ReminderStatus.CANCELLED
        return _detach(s, t)


def reopen_task(user_id: int, task_id: int) -> Task:
    with session_scope() as s:
        t = _get(s, Task, task_id, user_id)
        t.status, t.completed_at = TaskStatus.OPEN, None
        return _detach(s, t)


def cancel_task(user_id: int, task_id: int) -> Task:
    with session_scope() as s:
        t = _get(s, Task, task_id, user_id)
        t.status = TaskStatus.CANCELLED
        for r in s.scalars(select(Reminder).where(Reminder.task_id == t.id,
                                                  Reminder.status == ReminderStatus.PENDING)):
            r.status = ReminderStatus.CANCELLED
        return _detach(s, t)


_PRIORITY_RANK = {Priority.URGENT: 0, Priority.HIGH: 1, Priority.MEDIUM: 2, Priority.LOW: 3}


def open_tasks(user_id: int) -> list[Task]:
    with session_scope() as s:
        rows = s.scalars(select(Task).where(Task.user_id == user_id, Task.status == TaskStatus.OPEN)).all()
        for r in rows:
            s.expunge(r)
    far = datetime.max.replace(tzinfo=utcnow().tzinfo)
    return sorted(rows, key=lambda t: (t.due_at or far, _PRIORITY_RANK[t.priority], t.id))


def tasks_between(user_id: int, start: datetime, end: datetime, *, status: TaskStatus | None = None,
                  field: str = "completed_at") -> list[Task]:
    col = getattr(Task, field)
    with session_scope() as s:
        q = select(Task).where(Task.user_id == user_id, col >= start, col < end)
        if status is not None:
            q = q.where(Task.status == status)
        rows = s.scalars(q.order_by(col)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------
def create_reminder(user_id: int, message: str, remind_at: datetime, *, task_id: int | None = None,
                    followup_id: int | None = None) -> Reminder:
    message = message.strip()[:1000]
    if not message:
        raise ValueError("A reminder needs some text")
    if remind_at <= utcnow():
        raise ValueError("That time has already passed")
    with session_scope() as s:
        r = Reminder(user_id=user_id, message=message, remind_at=remind_at, task_id=task_id,
                     followup_id=followup_id)
        s.add(r)
        return _detach(s, r)


def pending_reminders(user_id: int) -> list[Reminder]:
    with session_scope() as s:
        rows = s.scalars(select(Reminder).where(
            Reminder.user_id == user_id, Reminder.status == ReminderStatus.PENDING
        ).order_by(Reminder.remind_at)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def update_reminder(user_id: int, reminder_id: int, *, message: str | None = None,
                    remind_at: datetime | None = None) -> Reminder:
    with session_scope() as s:
        r = _get(s, Reminder, reminder_id, user_id)
        if r.status != ReminderStatus.PENDING:
            raise ValueError(f"Reminder #{reminder_id} is already {r.status.value}")
        if message is not None:
            r.message = message.strip()[:1000]
        if remind_at is not None:
            if remind_at <= utcnow():
                raise ValueError("That time has already passed")
            r.remind_at = remind_at
        return _detach(s, r)


def cancel_reminder(user_id: int, reminder_id: int) -> Reminder:
    with session_scope() as s:
        r = _get(s, Reminder, reminder_id, user_id)
        if r.status == ReminderStatus.PENDING:
            r.status = ReminderStatus.CANCELLED
        return _detach(s, r)


# --------------------------------------------------------------------------
# Follow-ups (never contacts anyone; it only tracks)
# --------------------------------------------------------------------------
NEGLECT_DAYS = 14


def create_followup(user_id: int, person: str, *, context: str | None = None,
                    next_at: datetime | None = None, channel: str | None = None,
                    priority: Priority = Priority.MEDIUM) -> FollowUp:
    person = person.strip()[:200]
    if not person:
        raise ValueError("Who should you follow up with?")
    with session_scope() as s:
        f = FollowUp(user_id=user_id, person_name=person, context=(context or "").strip()[:2000] or None,
                     next_follow_up_at=next_at, contact_channel=channel, priority=priority)
        s.add(f)
        return _detach(s, f)


def update_followup(user_id: int, followup_id: int, **fields) -> FollowUp:
    allowed = {"person_name", "context", "next_follow_up_at", "contact_channel", "priority"}
    if set(fields) - allowed:
        raise ValueError("Unsupported follow-up field")
    with session_scope() as s:
        f = _get(s, FollowUp, followup_id, user_id)
        for k, v in fields.items():
            setattr(f, k, v)
        return _detach(s, f)


def mark_contacted(user_id: int, followup_id: int, next_at: datetime | None = None) -> FollowUp:
    """Record that *the owner* made contact. Done unless a next date is given."""
    with session_scope() as s:
        f = _get(s, FollowUp, followup_id, user_id)
        f.last_contact_at = utcnow()
        f.next_follow_up_at = next_at
        f.status = FollowUpStatus.PENDING if next_at else FollowUpStatus.DONE
        return _detach(s, f)


def cancel_followup(user_id: int, followup_id: int) -> FollowUp:
    with session_scope() as s:
        f = _get(s, FollowUp, followup_id, user_id)
        f.status = FollowUpStatus.CANCELLED
        return _detach(s, f)


def pending_followups(user_id: int) -> list[FollowUp]:
    with session_scope() as s:
        rows = s.scalars(select(FollowUp).where(
            FollowUp.user_id == user_id, FollowUp.status == FollowUpStatus.PENDING)).all()
        for r in rows:
            s.expunge(r)
    far = datetime.max.replace(tzinfo=utcnow().tzinfo)
    return sorted(rows, key=lambda f: (f.next_follow_up_at or far, f.id))


def is_neglected(f: FollowUp, now: datetime | None = None) -> bool:
    """No date set and no contact (or creation) for NEGLECT_DAYS."""
    now = now or utcnow()
    if f.next_follow_up_at is not None:
        return False
    anchor = f.last_contact_at or f.created_at
    return now - anchor >= timedelta(days=NEGLECT_DAYS)


# --------------------------------------------------------------------------
# Memory (explicitly provided facts only)
# --------------------------------------------------------------------------
_CATEGORY_HINTS = [
    (MemoryCategory.OBJECTIVE, r"\b(goal|objective|aim|aspire|long[- ]term|want to become|plan to)\b"),
    (MemoryCategory.STUDY, r"\b(study|studying|exam|course|certification|cert|learn(ing)?)\b"),
    (MemoryCategory.CONSTRAINT, r"\b(can'?t|cannot|unavailable|only (available|free)|busy|no meetings)\b"),
    (MemoryCategory.COMMUNICATION, r"\b(prefer|preference|concise|tone|call me|don'?t (message|text))\b"),
    (MemoryCategory.COMMITMENT, r"\b(committed|promise[d]?|agreed to|deadline|obligation)\b"),
    (MemoryCategory.PROJECT, r"\b(project|building|working on)\b"),
]


def guess_category(text: str) -> MemoryCategory:
    for cat, pattern in _CATEGORY_HINTS:
        if re.search(pattern, text, re.IGNORECASE):
            return cat
    return MemoryCategory.OTHER


def add_memory(user_id: int, content: str, category: MemoryCategory | None = None) -> MemoryItem:
    content = content.strip()[:2000]
    if not content:
        raise ValueError("Nothing to remember")
    with session_scope() as s:
        m = MemoryItem(user_id=user_id, content=content, category=category or guess_category(content))
        s.add(m)
        item = _detach(s, m)
    audit(AuditEvent.DATA_CHANGED, None, entity="memory", id=item.id, op="create")
    return item


def update_memory(user_id: int, memory_id: int, *, content: str | None = None,
                  category: MemoryCategory | None = None) -> MemoryItem:
    with session_scope() as s:
        m = _get(s, MemoryItem, memory_id, user_id)
        if not m.is_active:
            raise NotFound(f"No memory #{memory_id}")
        if content is not None:
            m.content = content.strip()[:2000]
        if category is not None:
            m.category = category
        return _detach(s, m)


def list_memory(user_id: int, category: MemoryCategory | None = None) -> list[MemoryItem]:
    with session_scope() as s:
        q = select(MemoryItem).where(MemoryItem.user_id == user_id, MemoryItem.is_active.is_(True))
        if category:
            q = q.where(MemoryItem.category == category)
        rows = s.scalars(q.order_by(MemoryItem.category, MemoryItem.id)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def forget_memory(user_id: int, memory_id: int | None = None, category: MemoryCategory | None = None,
                  everything: bool = False) -> int:
    """Hard delete: forgotten means gone, not hidden."""
    if not (memory_id or category or everything):
        raise ValueError("Specify an item, a category or everything")
    with session_scope() as s:
        q = select(MemoryItem).where(MemoryItem.user_id == user_id)
        if memory_id:
            q = q.where(MemoryItem.id == memory_id)
        elif category:
            q = q.where(MemoryItem.category == category)
        rows = s.scalars(q).all()
        if memory_id and not rows:
            raise NotFound(f"No memory #{memory_id}")
        for r in rows:
            s.delete(r)
        n = len(rows)
    audit(AuditEvent.DATA_DELETED, None, entity="memory", count=n,
          scope="all" if everything else ("category" if category else "item"))
    return n


def search_text(items, needle: str):
    needle = needle.lower()
    return [i for i in items if needle in (getattr(i, "title", None) or getattr(i, "content", "") or
                                           getattr(i, "person_name", "")).lower()]


