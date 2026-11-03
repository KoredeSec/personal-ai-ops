"""HTML rendering for Chief of Staff replies. Every dynamic value is escaped."""

from __future__ import annotations

from datetime import datetime

from app.agents.chief_of_staff.planning import Briefing, WeeklyReview
from app.bot.formatting import esc
from app.core.timeutils import format_local, to_local, utcnow
from app.database.models import FollowUp, MemoryItem, Priority, Reminder, Task

_PRIO_TAG = {Priority.URGENT: " · <b>urgent</b>", Priority.HIGH: " · high", Priority.MEDIUM: "", Priority.LOW: " · low"}


def when_short(dt: datetime | None, now: datetime | None = None) -> str:
    """'today 18:00', 'tomorrow 09:00', 'Fri 06 Nov 09:00', 'Sun 15 Nov 2027'."""
    if dt is None:
        return ""
    local, ref = to_local(dt), to_local(now or utcnow())
    days = (local.date() - ref.date()).days
    if days == 0:
        return f"today {local:%H:%M}"
    if days == 1:
        return f"tomorrow {local:%H:%M}"
    if days == -1:
        return f"yesterday {local:%H:%M}"
    if local.year != ref.year:
        return f"{local:%a %d %b %Y %H:%M}"
    return f"{local:%a %d %b %H:%M}"


def task_line(t: Task, now: datetime | None = None) -> str:
    due = f" — due {esc(when_short(t.due_at, now))}" if t.due_at else ""
    return f"#{t.id} {esc(t.title)}{due}{_PRIO_TAG[t.priority]}"


def reminder_line(r: Reminder, now: datetime | None = None) -> str:
    return f"#{r.id} {esc(when_short(r.remind_at, now))} — {esc(r.message)}"


def followup_line(f: FollowUp, now: datetime | None = None) -> str:
    bits = [f"#{f.id} <b>{esc(f.person_name)}</b>"]
    if f.context:
        bits.append(f"— {esc(f.context)}")
    if f.next_follow_up_at:
        bits.append(f"· {esc(when_short(f.next_follow_up_at, now))}")
    if f.last_contact_at:
        bits.append(f"· last contact {esc(format_local(f.last_contact_at, with_time=False))}")
    return " ".join(bits)


def memory_line(m: MemoryItem) -> str:
    return f"#{m.id} [{esc(m.category.value)}] {esc(m.content)}"


def _section(title: str, lines: list[str]) -> str:
    return f"<b>{esc(title)}</b>\n" + "\n".join(lines) if lines else ""


def _duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def briefing(b: Briefing, now: datetime | None = None) -> str:
    title = "Today" if b.is_today else "Plan"
    head = f"📋 <b>{title} · {b.day:%a %d %b}</b>"
    parts = [head]
    if b.priorities:
        parts.append(_section("Top priorities", [f"{i}. {task_line(t, now)}" for i, t in enumerate(b.priorities, 1)]))
    if b.overdue:
        parts.append(_section(f"Overdue ({len(b.overdue)}) — reschedule or drop",
                              [task_line(t, now) for t in b.overdue[:8]]))
    if b.due_today:
        parts.append(_section("Due" if b.is_today else "Due that day", [task_line(t, now) for t in b.due_today[:10]]))
    if b.reminders:
        parts.append(_section("Reminders", [reminder_line(r, now) for r in b.reminders]))
    fu = [followup_line(f, now) for f in b.followups_due]
    fu += [followup_line(f, now) + " · <i>no contact in 14+ days</i>" for f in b.followups_neglected
           if f not in b.followups_due]
    if fu:
        parts.append(_section("Follow-ups", fu))
    if b.conflicts:
        parts.append(_section("⚠️ Conflicts", [esc(c.message) for c in b.conflicts]))
    if b.upcoming:
        parts.append(_section("Next 7 days", [task_line(t, now) for t in b.upcoming[:6]]))
    free = b.free
    if free.window:
        parts.append(f"<b>Time for personal development</b>: ~{_duration(free.minutes)}\n"
                     f"<i>Assumes {esc('; '.join(free.assumptions))}. Change with /settings.</i>")
    if b.system_issues:
        parts.append(_section("System", [esc(s) for s in b.system_issues] + ["Details: /status"]))
    if b.is_empty:
        parts.insert(1, "Nothing due, overdue or scheduled.")
    return "\n\n".join(p for p in parts if p)


def weekly(w: WeeklyReview, priorities: list[tuple[str, str]], ai_used: bool, now: datetime | None = None) -> str:
    parts = [f"🗓 <b>Week review · {to_local(w.start):%d %b} – {to_local(w.end):%d %b}</b>"]
    d = w.decisions
    parts.append(
        "<b>Decisions</b>\n"
        f"Taken off your plate: {d.removed} · handed back to you: {d.added} · net {d.net:+d}\n"
        f"Open loops: {w.loops_opened} opened, {w.loops_closed} closed (net {w.net_loops:+d})"
        + ("\n<i>More loops opened than closed: the system created more work than it absorbed.</i>"
           if w.net_loops > 0 else "")
    )
    parts.append(_section(f"Completed ({len(w.completed)})", [task_line(t, now) for t in w.completed[:10]])
                 or "<b>Completed</b>\nNothing completed this week.")
    if w.missed:
        parts.append(_section(f"Missed deadlines ({len(w.missed)})", [task_line(t, now) for t in w.missed[:8]]))
    if w.unfinished:
        parts.append(_section(f"Still open ({len(w.unfinished)})", [task_line(t, now) for t in w.unfinished[:8]]))
    upcoming = [task_line(t, now) for t in w.upcoming_tasks[:6]]
    upcoming += [followup_line(f, now) for f in w.upcoming_followups[:4]]
    upcoming += [f"{esc(n)} — {esc(when_short(dt, now))}{'' if v else ' <i>(unverified)</i>'}"
                 for n, dt, v in w.upcoming_opportunity_deadlines[:4]]
    if upcoming:
        parts.append(_section("Coming up", upcoming))
    if w.objectives or w.project_progress:
        lines = [f"• {esc(o)}" for o in w.objectives[:5]]
        lines += [f"• {esc(n)}: {done} done this week, {left} open" for n, done, left in w.project_progress]
        parts.append(_section("Long-term objectives", lines))
    if priorities:
        label = "Suggested priorities for next week" + ("" if ai_used else " (by deadline and priority)")
        parts.append(_section(label, [f"{i}. {esc(t)}" + (f" — <i>{esc(r)}</i>" if r else "")
                                      for i, (t, r) in enumerate(priorities, 1)]))
    parts.append(f"<b>System</b>\nAI requests: {w.ai_requests} · errors: {w.ai_errors}")
    return "\n\n".join(p for p in parts if p)
