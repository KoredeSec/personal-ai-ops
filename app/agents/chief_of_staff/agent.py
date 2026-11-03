"""Chief of Staff executor: turns an Intent into a deterministic action + reply.

Telegram-agnostic. Returns a Reply (HTML + button rows + optional pending
state). The bot layer only renders it. This is also the single place that
decides whether a request "removed a decision" or "handed one back".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from app.agents.chief_of_staff import decisions, planning, render, store
from app.agents.chief_of_staff.dates import WhenError, extract_when, parse_when
from app.agents.chief_of_staff.intents import Intent
from app.agents.chief_of_staff.preferences import Prefs
from app.bot.formatting import esc
from app.core.llm import GeminiClient, LLMError
from app.core.timeutils import format_local, to_local, utcnow
from app.database.models import MemoryCategory, Priority

logger = logging.getLogger(__name__)

Button = tuple[str, str]  # (label, callback_data)


@dataclass
class Reply:
    html: str
    buttons: list[list[Button]] = field(default_factory=list)
    pending: dict | None = None  # state for the next message (e.g. waiting for a time)
    document: tuple[str, bytes] | None = None  # (filename, content)


@dataclass
class Ctx:
    user_id: int
    prefs: Prefs
    llm: GeminiClient | None
    now: datetime  # aware UTC
    interactive_deadline: float = 20.0

    @property
    def local_now(self) -> datetime:
        return to_local(self.now)


def _when(dt: datetime) -> str:
    return esc(format_local(dt))


def _undo_edit(kind: str, item_id: int) -> list[Button]:
    return [("↩️ Undo", f"cos:undo:{kind}:{item_id}"), ("✏️ Edit", f"cos:edit:{kind}:{item_id}")]


RECURRING_MSG = ("Repeating reminders aren't supported yet, so I haven't set anything. "
                 "Send it as a one-off (e.g. \"remind me to file timesheet monday 9am\").")
EXAMPLES = ("Try: \"remind me to call mum tomorrow 6pm\", \"add task renew passport by 20 Nov\", "
            "\"follow up with Ade on friday\", \"what are my tasks\", /help")
RESEARCH_MSG = ("The Research Agent isn't available yet. For now I handle tasks, reminders, follow-ups, "
                "memory and planning.")

# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def execute(intent: Intent, ctx: Ctx) -> Reply:
    handler = _HANDLERS.get(intent.action, _unknown)
    return await handler(intent, ctx)


def _clarify(ctx: Ctx, html: str, kind: str = "clarification_asked", **kw) -> Reply:
    decisions.added(ctx.user_id, kind)
    return Reply(html, **kw)


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------
async def _add_reminder(i: Intent, ctx: Ctx) -> Reply:
    if i.recurring:
        return _clarify(ctx, esc(RECURRING_MSG))
    if i.when_error:
        return _clarify(ctx, esc(f"{i.when_error}. When should I remind you?"),
                        pending={"op": "need_time", "intent": "add_reminder", "text": i.text,
                                 "person": i.person, "context": i.context})
    if not i.text:
        return _clarify(ctx, "What should I remind you about?")
    if i.when is None:
        return _clarify(ctx, f"When should I remind you to <b>{esc(i.text)}</b>? (e.g. tomorrow 9am, friday, in 2 hours)",
                        pending={"op": "need_time", "intent": "add_reminder", "text": i.text,
                                 "person": i.person, "context": i.context})
    at_utc = i.when.at.astimezone(utcnow().tzinfo)
    followup_id = None
    extra = ""
    if i.person:
        fu = store.create_followup(ctx.user_id, i.person, context=i.context, next_at=at_utc)
        followup_id = fu.id
        extra = f"\nAlso tracking a follow-up with <b>{esc(fu.person_name)}</b> (#{fu.id})."
    r = store.create_reminder(ctx.user_id, i.text, at_utc, followup_id=followup_id)
    decisions.removed(ctx.user_id, "request_handled", f"reminder:{r.id}")
    return Reply(f"⏰ Reminder #{r.id}: <b>{esc(r.message)}</b>\n{_when(r.remind_at)}{extra}",
                 [_undo_edit("rem", r.id)])


async def _add_task(i: Intent, ctx: Ctx) -> Reply:
    if i.recurring:
        return _clarify(ctx, esc("Repeating tasks aren't supported yet, so I haven't added it. "
                                 "Add it once with a date and I'll track it."))
    if i.when_error:
        return _clarify(ctx, esc(f"{i.when_error}. Send the task again with a valid date, or without one."))
    if not i.text:
        return _clarify(ctx, "What's the task?")
    due = i.when.at.astimezone(utcnow().tzinfo) if i.when else None
    t = store.create_task(ctx.user_id, i.text, due_at=due, priority=i.priority or Priority.MEDIUM)
    decisions.removed(ctx.user_id, "request_handled", f"task:{t.id}")
    due_txt = f"\nDue {_when(t.due_at)}" if t.due_at else ""
    prio = f" · {t.priority.value}" if t.priority != Priority.MEDIUM else ""
    buttons = [_undo_edit("task", t.id), [("✅ Done", f"cos:done:task:{t.id}")]]
    if t.due_at:
        buttons[1].append(("⏰ Remind me", f"cos:remindat:task:{t.id}"))
    return Reply(f"✅ Task #{t.id}: <b>{esc(t.title)}</b>{prio}{due_txt}", buttons)


async def _add_followup(i: Intent, ctx: Ctx) -> Reply:
    if i.when_error:
        return _clarify(ctx, esc(f"{i.when_error}. Send it again with a valid date, or without one."))
    if not i.person:
        return _clarify(ctx, "Who do you need to follow up with?")
    at = i.when.at.astimezone(utcnow().tzinfo) if i.when else None
    f = store.create_followup(ctx.user_id, i.person, context=i.context, next_at=at)
    decisions.removed(ctx.user_id, "request_handled", f"followup:{f.id}")
    when = (f"\nFollow up {_when(f.next_follow_up_at)}" if f.next_follow_up_at
            else f"\nNo date set. I'll flag it if {store.NEGLECT_DAYS} days pass without contact.")
    ctx_txt = f" — {esc(f.context)}" if f.context else ""
    return Reply(f"👤 Follow-up #{f.id}: <b>{esc(f.person_name)}</b>{ctx_txt}{when}\n"
                 "<i>I'll remind you; I never contact anyone myself.</i>",
                 [_undo_edit("fu", f.id)])


async def _remember(i: Intent, ctx: Ctx) -> Reply:
    if not i.text:
        return _clarify(ctx, "What should I remember?")
    m = store.add_memory(ctx.user_id, i.text)
    decisions.removed(ctx.user_id, "request_handled", f"memory:{m.id}")
    cats = [[(c.value, f"cos:cat:mem:{m.id}:{c.value}") for c in list(MemoryCategory)[i:i + 4]]
            for i in range(0, len(MemoryCategory), 4)]
    return Reply(f"🧠 Noted #{m.id} under <b>{esc(m.category.value)}</b>: {esc(m.content)}\n"
                 "<i>Wrong category? Tap the right one.</i>",
                 [[("↩️ Undo", f"cos:undo:mem:{m.id}")], *cats])


async def _complete(i: Intent, ctx: Ctx) -> Reply:
    if not i.task_id:
        return _clarify(ctx, "Which task number? e.g. \"done 12\". See /tasks.")
    try:
        t = store.complete_task(ctx.user_id, i.task_id)
    except store.NotFound:
        return Reply(f"There's no task #{i.task_id}. See /tasks.")
    decisions.removed(ctx.user_id, "request_handled", f"task:{t.id}")
    return Reply(f"✔️ Done: #{t.id} {esc(t.title)}", [[("↩️ Undo", f"cos:reopen:task:{t.id}")]])


# --------------------------------------------------------------------------
# Lists
# --------------------------------------------------------------------------
async def _list_tasks(i: Intent, ctx: Ctx, overdue_only: bool = False) -> Reply:
    tasks = store.open_tasks(ctx.user_id)
    if overdue_only:
        tasks = [t for t in tasks if t.due_at and t.due_at < ctx.now]
    if not tasks:
        return Reply("Nothing overdue." if overdue_only else "No open tasks. Add one: \"add task …\"")
    overdue = [t for t in tasks if t.due_at and t.due_at < ctx.now]
    dated = [t for t in tasks if t.due_at and t.due_at >= ctx.now]
    undated = [t for t in tasks if not t.due_at]
    parts = []
    for title, group in (("Overdue", overdue), ("Upcoming", dated), ("No date", undated)):
        if group:
            parts.append(f"<b>{title} ({len(group)})</b>\n" + "\n".join(render.task_line(t, ctx.now) for t in group[:15]))
    shown = (overdue + dated + undated)[:8]
    rows = [[(f"✅ #{t.id}", f"cos:done:task:{t.id}") for t in shown[k:k + 4]] for k in range(0, len(shown), 4)]
    return Reply("\n\n".join(parts) + "\n\n<i>Tap to mark done, or send \"done 12\".</i>", rows)


async def _list_overdue(i: Intent, ctx: Ctx) -> Reply:
    return await _list_tasks(i, ctx, overdue_only=True)


async def _list_reminders(i: Intent, ctx: Ctx) -> Reply:
    rs = store.pending_reminders(ctx.user_id)
    if not rs:
        return Reply("No pending reminders.")
    rows = [[(f"✖️ Cancel #{r.id}", f"cos:cancel:rem:{r.id}"), (f"✏️ #{r.id}", f"cos:edit:rem:{r.id}")]
            for r in rs[:8]]
    return Reply("<b>Pending reminders</b>\n" + "\n".join(render.reminder_line(r, ctx.now) for r in rs[:20]), rows)


async def _list_followups(i: Intent, ctx: Ctx) -> Reply:
    fus = store.pending_followups(ctx.user_id)
    if not fus:
        return Reply("No pending follow-ups.")
    lines = []
    for f in fus[:20]:
        flag = " · <i>neglected</i>" if store.is_neglected(f, ctx.now) else ""
        due = " · <b>due</b>" if f.next_follow_up_at and f.next_follow_up_at <= ctx.now else ""
        lines.append(render.followup_line(f, ctx.now) + due + flag)
    rows = [[(f"☑️ Contacted #{f.id}", f"cos:contacted:fu:{f.id}")] for f in fus[:8]]
    return Reply("<b>Follow-ups</b>\n" + "\n".join(lines) +
                 "\n\n<i>Tap when you've made contact. I never message anyone for you.</i>", rows)


async def _list_memory(i: Intent, ctx: Ctx) -> Reply:
    items = store.list_memory(ctx.user_id)
    if not items:
        return Reply("I'm not storing anything about you yet. Tell me with \"remember that …\".")
    rows = [[(f"🗑 #{m.id}", f"cos:undo:mem:{m.id}") for m in items[k:k + 4]] for k in range(0, min(len(items), 12), 4)]
    return Reply("<b>What I remember</b> (only what you told me)\n" +
                 "\n".join(render.memory_line(m) for m in items) +
                 "\n\n<i>Tap to delete. /forget all deletes everything. Edit: /memory edit 3 new text</i>", rows)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def briefing_reply(ctx: Ctx, day, record: bool = True) -> Reply:
    b = planning.build_briefing(ctx.user_id, ctx.prefs, day, ctx.now)
    if record:
        planning.record_briefing_decisions(ctx.user_id, b)
    rows = [[(f"✅ #{t.id}", f"cos:done:task:{t.id}") for t in b.priorities]] if b.priorities else []
    return Reply(render.briefing(b, ctx.now), rows)


async def _briefing_today(i: Intent, ctx: Ctx) -> Reply:
    return briefing_reply(ctx, ctx.local_now.date())


async def _plan_tomorrow(i: Intent, ctx: Ctx) -> Reply:
    return briefing_reply(ctx, ctx.local_now.date() + timedelta(days=1))


class _Suggestion(BaseModel):
    title: str = Field(description="A task title from the list, or a short action tied to an objective")
    reason: str = Field(description="One short, concrete reason; reference the objective or deadline")


class _Suggestions(BaseModel):
    priorities: list[_Suggestion] = Field(max_length=3)


async def weekly_reply(ctx: Ctx) -> Reply:
    w = planning.build_weekly(ctx.user_id, ctx.now)
    suggestions: list[tuple[str, str]] = []
    ai_used = False
    if ctx.llm and ctx.llm.configured and (w.unfinished or w.objectives):
        try:
            prompt = json.dumps({
                "objectives": w.objectives[:8],
                "open_tasks": [{"title": t.title, "due": format_local(t.due_at) if t.due_at else None,
                                "priority": t.priority.value} for t in w.unfinished[:20]],
                "missed_this_week": [t.title for t in w.missed[:10]],
            }, ensure_ascii=False)
            out = await ctx.llm.generate_json(
                "Pick at most 3 priorities for next week from this data. Prefer items that advance an "
                "objective or have a near deadline. No motivational language. Data:\n" + prompt,
                _Suggestions, tier="primary", temperature=0.2, max_output_tokens=1024,
                agent="chief_of_staff", action="weekly_priorities", user_id=ctx.user_id,
                deadline_seconds=ctx.interactive_deadline)
            suggestions = [(s.title[:200], s.reason[:200]) for s in out.priorities[:3]]
            ai_used = bool(suggestions)
        except LLMError as exc:
            logger.warning("Weekly priorities fell back to deterministic ranking: %s", type(exc).__name__)
    if not suggestions:
        suggestions = [(f"#{t.id} {t.title}", "") for t in w.fallback_priorities]
    return Reply(render.weekly(w, suggestions, ai_used, ctx.now))


async def _weekly(i: Intent, ctx: Ctx) -> Reply:
    return await weekly_reply(ctx)


# --------------------------------------------------------------------------
# Other
# --------------------------------------------------------------------------
async def _research(i: Intent, ctx: Ctx) -> Reply:
    return Reply(esc(RESEARCH_MSG))


async def _help(i: Intent, ctx: Ctx) -> Reply:
    from app.bot.handlers.basic import HELP_TEXT

    return Reply(HELP_TEXT)


async def _unknown(i: Intent, ctx: Ctx) -> Reply:
    return _clarify(ctx, "I'm not sure what you'd like me to do with that.\n" + esc(EXAMPLES))


_HANDLERS = {
    "add_reminder": _add_reminder, "add_task": _add_task, "add_followup": _add_followup,
    "remember": _remember, "complete_task": _complete, "list_tasks": _list_tasks,
    "list_overdue": _list_overdue, "list_reminders": _list_reminders,
    "list_followups": _list_followups, "list_memory": _list_memory,
    "briefing_today": _briefing_today, "plan_tomorrow": _plan_tomorrow, "weekly": _weekly,
    "research": _research, "help": _help, "unknown": _unknown,
}


# --------------------------------------------------------------------------
# Button actions and edits
# --------------------------------------------------------------------------
def button_action(ctx: Ctx, op: str, kind: str, item_id: int, arg: str | None = None) -> Reply:
    """Execute a callback button. Ownership is enforced by the store layer."""
    uid = ctx.user_id
    try:
        if op == "undo":
            decisions.added(uid, "interpretation_fixed", f"{kind}:{item_id}")
            if kind == "task":
                t = store.cancel_task(uid, item_id)
                return Reply(f"↩️ Removed task #{t.id} {esc(t.title)}.")
            if kind == "rem":
                r = store.cancel_reminder(uid, item_id)
                if r.followup_id:
                    store.cancel_followup(uid, r.followup_id)
                return Reply(f"↩️ Cancelled reminder #{r.id}.")
            if kind == "fu":
                f = store.cancel_followup(uid, item_id)
                return Reply(f"↩️ Removed follow-up #{f.id} ({esc(f.person_name)}).")
            if kind == "mem":
                store.forget_memory(uid, memory_id=item_id)
                return Reply(f"🗑 Deleted memory #{item_id}.")
        if op == "done" and kind == "task":
            t = store.complete_task(uid, item_id)
            decisions.removed(uid, "request_handled", f"task:{t.id}")
            return Reply(f"✔️ Done: #{t.id} {esc(t.title)}", [[("↩️ Undo", f"cos:reopen:task:{t.id}")]])
        if op == "reopen" and kind == "task":
            t = store.reopen_task(uid, item_id)
            return Reply(f"↩️ Reopened #{t.id} {esc(t.title)}.")
        if op == "cancel" and kind == "rem":
            r = store.cancel_reminder(uid, item_id)
            return Reply(f"✖️ Cancelled reminder #{r.id}: {esc(r.message)}")
        if op == "contacted" and kind == "fu":
            f = store.mark_contacted(uid, item_id)
            decisions.removed(uid, "request_handled", f"followup:{f.id}")
            return Reply(f"☑️ Logged contact with {esc(f.person_name)}. Closed #{f.id}.\n"
                         f"<i>Need another check-in? \"follow up with {esc(f.person_name)} in 2 weeks\"</i>")
        if op == "remindat" and kind == "task":
            t = store.get_task(uid, item_id)
            if not t.due_at or t.due_at <= ctx.now:
                return Reply("That task has no future due time to remind you at.")
            r = store.create_reminder(uid, t.title, t.due_at, task_id=t.id)
            return Reply(f"⏰ Reminder #{r.id} set for {_when(r.remind_at)}.", [[("↩️ Undo", f"cos:undo:rem:{r.id}")]])
        if op == "cat" and kind == "mem" and arg:
            m = store.update_memory(uid, item_id, category=MemoryCategory(arg))
            decisions.added(uid, "interpretation_fixed", f"memory:{m.id}")
            return Reply(f"🧠 Moved #{m.id} to <b>{esc(m.category.value)}</b>.")
        if op == "edit":
            return Reply(f"Send the corrected version of #{item_id}. Include the new date/time if it changed.\n"
                         "<i>e.g. \"call mentor tuesday 10am\"</i>",
                         pending={"op": "edit", "kind": kind, "id": item_id})
        if op == "forgetall":
            n = store.forget_memory(uid, everything=True)
            return Reply(f"🗑 Deleted all {n} memory item(s).")
    except store.NotFound:
        return Reply("That item no longer exists.")
    except ValueError as exc:
        return Reply(esc(str(exc)))
    return Reply("That button has expired.")


def apply_edit(ctx: Ctx, kind: str, item_id: int, text: str) -> Reply:
    """Re-interpret the owner's corrected text for an existing item."""
    try:
        ex = extract_when(text, ctx.local_now, ctx.prefs.default_time)
    except WhenError as exc:
        return Reply(esc(f"{exc}. Send the correction again."), pending={"op": "edit", "kind": kind, "id": item_id})
    new_text = ex.remainder or None
    at = ex.when.at.astimezone(utcnow().tzinfo) if ex.when else None
    decisions.added(ctx.user_id, "interpretation_fixed", f"{kind}:{item_id}")
    try:
        if kind == "task":
            fields = {}
            if new_text:
                fields["title"] = new_text
            if at:
                fields["due_at"] = at
            t = store.update_task(ctx.user_id, item_id, **fields)
            due = f"\nDue {_when(t.due_at)}" if t.due_at else ""
            return Reply(f"✏️ Task #{t.id}: <b>{esc(t.title)}</b>{due}", [_undo_edit("task", t.id)])
        if kind == "rem":
            r = store.update_reminder(ctx.user_id, item_id, message=new_text, remind_at=at)
            return Reply(f"✏️ Reminder #{r.id}: <b>{esc(r.message)}</b>\n{_when(r.remind_at)}", [_undo_edit("rem", r.id)])
        if kind == "fu":
            fields = {}
            if new_text:
                person, _, context = new_text.partition(" about ")
                fields["person_name"] = person.strip()
                if context:
                    fields["context"] = context.strip()
            if at:
                fields["next_follow_up_at"] = at
            f = store.update_followup(ctx.user_id, item_id, **fields)
            nxt = f"\nFollow up {_when(f.next_follow_up_at)}" if f.next_follow_up_at else ""
            return Reply(f"✏️ Follow-up #{f.id}: <b>{esc(f.person_name)}</b>{nxt}", [_undo_edit("fu", f.id)])
        if kind == "mem":
            m = store.update_memory(ctx.user_id, item_id, content=text)
            return Reply(f"✏️ Memory #{m.id}: {esc(m.content)}")
    except store.NotFound:
        return Reply("That item no longer exists.")
    except ValueError as exc:
        return Reply(esc(str(exc)))
    return Reply("I can't edit that.")


async def complete_pending_time(ctx: Ctx, pending: dict, text: str) -> Reply:
    """The owner answered 'when?' for an action that was waiting on a time."""
    try:
        when = parse_when(text, ctx.local_now, ctx.prefs.default_time)
        if when is None:
            ex = extract_when(text, ctx.local_now, ctx.prefs.default_time)
            when = ex.when
    except WhenError as exc:
        return Reply(esc(f"{exc}. When should it be?"), pending=pending)
    if when is None:
        return Reply("I still couldn't read a time. Try \"tomorrow 9am\" or \"friday 3pm\", "
                     "or send /cancel.", pending=pending)
    intent = Intent(pending["intent"], text=pending.get("text"), when=when,
                    person=pending.get("person"), context=pending.get("context"))
    return await execute(intent, ctx)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def export_json(user_id: int) -> bytes:
    from sqlalchemy import select

    from app.database.models import DecisionEvent, FollowUp, MemoryItem, Preference, Project, Reminder, Task
    from app.database.session import session_scope

    def rows(s, model):
        out = []
        for obj in s.scalars(select(model).where(model.user_id == user_id)):
            d = {}
            for col in model.__table__.columns:
                v = getattr(obj, col.name)
                d[col.name] = v.isoformat() if isinstance(v, datetime) else (v.value if hasattr(v, "value") else v)
            out.append(d)
        return out

    with session_scope() as s:
        data = {
            "exported_at": utcnow().isoformat(),
            "format": "personal-ai-ops/1",
            "tasks": rows(s, Task), "reminders": rows(s, Reminder), "followups": rows(s, FollowUp),
            "memory": rows(s, MemoryItem), "projects": rows(s, Project),
            "preferences": rows(s, Preference), "decision_events": rows(s, DecisionEvent),
        }
    return json.dumps(data, indent=2, ensure_ascii=False, default=str).encode()


