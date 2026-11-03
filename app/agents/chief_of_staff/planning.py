"""Deterministic planning: ranking, conflicts, free time, briefing/review data.

No model calls here. The briefing has to work when Gemini is down or out of
quota, and "what's due, what's overdue, what overlaps" is arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from app.agents.chief_of_staff import decisions, store
from app.agents.chief_of_staff.preferences import Prefs
from app.core.timeutils import local_day_bounds_utc, local_tz, to_local, utcnow
from app.database.models import (
    AgentExecutionLog,
    AuditLog,
    ExecutionStatus,
    FollowUp,
    MemoryCategory,
    Opportunity,
    OpportunityDeadline,
    Priority,
    Project,
    ProjectStatus,
    Reminder,
    Task,
    TaskStatus,
)
from app.database.session import session_scope

DEFAULT_TASK_MINUTES = 30
_WEIGHT = {Priority.URGENT: 40, Priority.HIGH: 25, Priority.MEDIUM: 10, Priority.LOW: 0}


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------
def score(task: Task, now: datetime) -> float:
    """Higher = do sooner. Priority plus deadline pressure; overdue dominates."""
    s = float(_WEIGHT[task.priority])
    if task.due_at is not None:
        hours = (task.due_at - now).total_seconds() / 3600
        if hours < 0:
            s += 50 + min(-hours / 24, 10)  # overdue, slightly more the longer it slips
        elif hours <= 24:
            s += 35
        elif hours <= 72:
            s += 20
        elif hours <= 168:
            s += 8
    return s


def rank(tasks: list[Task], now: datetime, limit: int = 3) -> list[Task]:
    return sorted(tasks, key=lambda t: (-score(t, now), t.id))[:limit]


# --------------------------------------------------------------------------
# Conflicts
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Conflict:
    kind: str  # "overlap" | "overload"
    message: str
    task_ids: tuple[int, ...]


def find_overlaps(tasks: list[Task]) -> list[Conflict]:
    blocks = sorted((t for t in tasks if t.scheduled_start and t.scheduled_end),
                    key=lambda t: t.scheduled_start)
    out = []
    for i, a in enumerate(blocks):
        for b in blocks[i + 1:]:
            if b.scheduled_start >= a.scheduled_end:
                break
            out.append(Conflict("overlap", f"#{a.id} {a.title} overlaps #{b.id} {b.title}", (a.id, b.id)))
    return out


# --------------------------------------------------------------------------
# Free time
# --------------------------------------------------------------------------
def _minutes(a: time, b: time) -> int:
    return (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)


def _interval_subtract(free: list[tuple[datetime, datetime]], busy: tuple[datetime, datetime]):
    out = []
    bs, be = busy
    for fs, fe in free:
        if be <= fs or bs >= fe:
            out.append((fs, fe))
            continue
        if bs > fs:
            out.append((fs, bs))
        if be < fe:
            out.append((be, fe))
    return out


@dataclass(frozen=True)
class FreeTime:
    minutes: int
    window: tuple[datetime, datetime] | None
    assumptions: list[str]


def free_time(day: date, prefs: Prefs, tasks_for_day: list[Task], now: datetime) -> FreeTime:
    """Awake window minus work hours, time blocks and estimated task effort."""
    tz = local_tz()
    day_start_t, day_end_t = (prefs.quiet[1], prefs.quiet[0]) if prefs.quiet else (time(7), time(22))
    start = datetime.combine(day, day_start_t, tzinfo=tz)
    end = datetime.combine(day, day_end_t, tzinfo=tz)
    if end <= start:  # quiet window that doesn't wrap midnight; fall back
        start, end = datetime.combine(day, time(7), tzinfo=tz), datetime.combine(day, time(22), tzinfo=tz)
    local_now = to_local(now)
    if local_now.date() == day:
        start = max(start, local_now)
    assumptions = [f"awake {day_start_t:%H:%M}-{day_end_t:%H:%M} (outside quiet hours)"]
    if start >= end:
        return FreeTime(0, None, assumptions)

    free = [(start, end)]
    work = prefs.work_window(day)
    if work:
        free = _interval_subtract(free, (datetime.combine(day, work[0], tzinfo=tz),
                                         datetime.combine(day, work[1], tzinfo=tz)))
        assumptions.append(f"work {work[0]:%H:%M}-{work[1]:%H:%M}")
    for t in tasks_for_day:
        if t.scheduled_start and t.scheduled_end:
            free = _interval_subtract(free, (to_local(t.scheduled_start), to_local(t.scheduled_end)))
    total = sum(int((e - s).total_seconds() // 60) for s, e in free)
    unscheduled = [t for t in tasks_for_day if not t.scheduled_start]
    effort = sum(t.estimated_minutes or DEFAULT_TASK_MINUTES for t in unscheduled)
    if unscheduled:
        assumptions.append(f"{effort} min for {len(unscheduled)} task(s) due"
                           + (f" ({DEFAULT_TASK_MINUTES} min each where no estimate)"
                              if any(t.estimated_minutes is None for t in unscheduled) else ""))
    return FreeTime(max(0, total - effort), (start, end), assumptions)


# --------------------------------------------------------------------------
# Daily briefing data
# --------------------------------------------------------------------------
@dataclass
class Briefing:
    day: date
    is_today: bool
    due_today: list[Task]
    overdue: list[Task]
    upcoming: list[Task]
    reminders: list[Reminder]
    followups_due: list[FollowUp]
    followups_neglected: list[FollowUp]
    priorities: list[Task]
    conflicts: list[Conflict]
    free: FreeTime
    system_issues: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.due_today or self.overdue or self.upcoming or self.reminders
                    or self.followups_due or self.followups_neglected)


def build_briefing(user_id: int, prefs: Prefs, day: date, now: datetime | None = None) -> Briefing:
    now = now or utcnow()
    start, end = local_day_bounds_utc(day)
    is_today = to_local(now).date() == day
    cutoff = now if is_today else start  # "overdue" relative to the plan's start

    tasks = store.open_tasks(user_id)
    due_today = [t for t in tasks if t.due_at and start <= t.due_at < end and t.due_at >= cutoff
                 or (t.scheduled_start and start <= t.scheduled_start < end)]
    overdue = [t for t in tasks if t.due_at and t.due_at < cutoff]
    upcoming = [t for t in tasks if t.due_at and end <= t.due_at < end + timedelta(days=7)]
    reminders = [r for r in store.pending_reminders(user_id) if start <= r.remind_at < end]
    fus = store.pending_followups(user_id)
    fu_due = [f for f in fus if f.next_follow_up_at and f.next_follow_up_at < end]
    fu_neglected = [f for f in fus if store.is_neglected(f, now)]

    conflicts = find_overlaps(due_today)
    free = free_time(day, prefs, due_today, now)
    if due_today and free.minutes == 0 and free.window:
        conflicts.append(Conflict("overload", "Estimated work for the day exceeds your free time",
                                  tuple(t.id for t in due_today)))

    candidates = list({t.id: t for t in overdue + due_today + upcoming}.values())
    return Briefing(
        day=day, is_today=is_today, due_today=due_today, overdue=overdue, upcoming=upcoming,
        reminders=reminders, followups_due=fu_due, followups_neglected=fu_neglected,
        priorities=rank(candidates, now), conflicts=conflicts, free=free,
        system_issues=recent_job_failures(now - timedelta(hours=24)),
    )


def record_briefing_decisions(user_id: int, b: Briefing) -> None:
    """A briefing removes one decision (what first?) and hands back the problems it found."""
    if b.priorities:
        decisions.removed(user_id, "briefing_ranked", f"briefing:{b.day.isoformat()}")
    for f in b.followups_due:
        decisions.removed(user_id, "followup_surfaced", f"followup:{f.id}")
    for t in b.overdue:
        decisions.added(user_id, "overdue_flagged", f"task:{t.id}")
    for c in b.conflicts:
        decisions.added(user_id, "conflict_flagged", ",".join(f"task:{i}" for i in c.task_ids))


def recent_job_failures(since: datetime) -> list[str]:
    with session_scope() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.event_type == "job_failed",
                                                AuditLog.created_at >= since)).all()
        return sorted({f"{r.details.get('job', '?')} failed" for r in rows})


# --------------------------------------------------------------------------
# Weekly review data
# --------------------------------------------------------------------------
@dataclass
class WeeklyReview:
    start: datetime
    end: datetime
    completed: list[Task]
    unfinished: list[Task]
    missed: list[Task]
    upcoming_tasks: list[Task]
    upcoming_followups: list[FollowUp]
    upcoming_opportunity_deadlines: list[tuple[str, datetime, bool]]
    objectives: list[str]
    project_progress: list[tuple[str, int, int]]  # name, done this week, open
    decisions: decisions.DecisionSummary
    loops_opened: int
    loops_closed: int
    ai_requests: int
    ai_errors: int
    fallback_priorities: list[Task]

    @property
    def net_loops(self) -> int:
        return self.loops_opened - self.loops_closed


def build_weekly(user_id: int, now: datetime | None = None) -> WeeklyReview:
    now = now or utcnow()
    start = now - timedelta(days=7)
    completed = store.tasks_between(user_id, start, now, status=TaskStatus.DONE, field="completed_at")
    open_ = store.open_tasks(user_id)
    with session_scope() as s:
        due_in_week = s.scalars(select(Task).where(Task.user_id == user_id, Task.due_at >= start,
                                                   Task.due_at < now)).all()
        missed = [t for t in due_in_week if t.status == TaskStatus.OPEN
                  or (t.status == TaskStatus.DONE and t.completed_at and t.completed_at > t.due_at)]
        for t in missed:
            s.expunge(t)
        created_tasks = s.scalar(select(func.count()).select_from(Task).where(
            Task.user_id == user_id, Task.created_at >= start, Task.created_at < now)) or 0
        closed_tasks = s.scalar(select(func.count()).select_from(Task).where(
            Task.user_id == user_id, Task.completed_at >= start, Task.completed_at < now)) or 0
        created_fu = s.scalar(select(func.count()).select_from(FollowUp).where(
            FollowUp.user_id == user_id, FollowUp.created_at >= start, FollowUp.created_at < now)) or 0
        closed_fu = s.scalar(select(func.count()).select_from(FollowUp).where(
            FollowUp.user_id == user_id, FollowUp.last_contact_at >= start,
            FollowUp.last_contact_at < now)) or 0
        projects = s.scalars(select(Project).where(Project.user_id == user_id,
                                                   Project.status == ProjectStatus.ACTIVE)).all()
        progress = []
        for p in projects:
            done = s.scalar(select(func.count()).select_from(Task).where(
                Task.project_id == p.id, Task.completed_at >= start, Task.completed_at < now)) or 0
            still = s.scalar(select(func.count()).select_from(Task).where(
                Task.project_id == p.id, Task.status == TaskStatus.OPEN)) or 0
            progress.append((p.name, done, still))
        opp = s.execute(
            select(Opportunity.name, OpportunityDeadline.deadline_at, OpportunityDeadline.is_verified)
            .join(OpportunityDeadline, OpportunityDeadline.opportunity_id == Opportunity.id)
            .where(OpportunityDeadline.deadline_at >= now,
                   OpportunityDeadline.deadline_at < now + timedelta(days=14))
            .order_by(OpportunityDeadline.deadline_at)).all()
        ai_req = s.scalar(select(func.count()).select_from(AgentExecutionLog).where(
            AgentExecutionLog.created_at >= start, AgentExecutionLog.cache_hit.is_(False),
            AgentExecutionLog.model.is_not(None))) or 0
        ai_err = s.scalar(select(func.count()).select_from(AgentExecutionLog).where(
            AgentExecutionLog.created_at >= start,
            AgentExecutionLog.status == ExecutionStatus.ERROR)) or 0

    next_week = now + timedelta(days=7)
    return WeeklyReview(
        start=start, end=now, completed=completed,
        unfinished=open_, missed=missed,
        upcoming_tasks=[t for t in open_ if t.due_at and now <= t.due_at < next_week],
        upcoming_followups=[f for f in store.pending_followups(user_id)
                            if f.next_follow_up_at and f.next_follow_up_at < next_week],
        upcoming_opportunity_deadlines=[(n, d, v) for n, d, v in opp],
        objectives=[m.content for m in store.list_memory(user_id, MemoryCategory.OBJECTIVE)],
        project_progress=progress,
        decisions=decisions.summarise(user_id, start, now),
        loops_opened=created_tasks + created_fu,
        loops_closed=closed_tasks + closed_fu,
        ai_requests=ai_req, ai_errors=ai_err,
        fallback_priorities=rank(open_, next_week - timedelta(days=6)),
    )
