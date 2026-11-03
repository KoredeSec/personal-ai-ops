"""Telegram handlers for the Chief of Staff and the natural-language router.

Thin by design: parse the update, build a Ctx, call the agent, render the
Reply. All behaviour lives in app/agents/chief_of_staff.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.agents.chief_of_staff import agent, store
from app.agents.chief_of_staff.intents import Intent, parse
from app.agents.chief_of_staff.nlu import classify
from app.agents.chief_of_staff.preferences import (
    DEFAULTS,
    DESCRIPTIONS,
    PreferenceError,
    get_prefs,
    reset_pref,
    set_pref,
)
from app.bot.formatting import esc
from app.bot.reply import reply_html, typing
from app.core.config import Settings
from app.core.llm import GeminiClient, LLMError
from app.core.security import sanitize_user_text
from app.core.timeutils import utcnow
from app.database.models import MemoryCategory
from app.services.audit import AuditEvent, audit
from app.services.users import get_or_create_user

logger = logging.getLogger(__name__)

PENDING_KEY = "pending"
SETTING_ALIASES = {"briefing": "briefing_time", "weekly": "weekly_review", "quiet": "quiet_hours",
                   "default": "default_time", "work": "work_hours"}


def _ctx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> agent.Ctx:
    u = update.effective_user
    assert u
    user = get_or_create_user(u.id, update.effective_chat.id if update.effective_chat else None, u.full_name)
    settings: Settings = context.application.bot_data["settings"]
    llm: GeminiClient = context.application.bot_data["llm"]
    return agent.Ctx(user_id=user.id, prefs=get_prefs(user.id), llm=llm, now=utcnow(),
                     interactive_deadline=settings.gemini_interactive_deadline_seconds)


def _markup(reply: agent.Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                 for row in reply.buttons if row])


async def send(update: Update, context: ContextTypes.DEFAULT_TYPE, reply: agent.Reply) -> None:
    if reply.pending is not None:
        context.user_data[PENDING_KEY] = reply.pending
    if reply.document and update.effective_message:
        name, content = reply.document
        await update.effective_message.reply_document(InputFile(content, filename=name), caption=reply.html[:1000])
        return
    await reply_html(update, reply.html, reply_markup=_markup(reply))


async def _run(update: Update, context: ContextTypes.DEFAULT_TYPE, intent: Intent) -> None:
    ctx = _ctx(update, context)
    context.user_data.pop(PENDING_KEY, None)
    await send(update, context, await agent.execute(intent, ctx))


# --------------------------------------------------------------------------
# Natural language
# --------------------------------------------------------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    assert msg
    text = sanitize_user_text(msg.text)
    if not text:
        return
    ctx = _ctx(update, context)

    pending = context.user_data.pop(PENDING_KEY, None)
    if pending:
        if pending.get("op") == "edit":
            await send(update, context, agent.apply_edit(ctx, pending["kind"], int(pending["id"]), text))
            return
        if pending.get("op") == "need_time":
            await send(update, context, await agent.complete_pending_time(ctx, pending, text))
            return

    intent = parse(text, ctx.local_now, ctx.prefs.default_time)
    if intent.action == "unknown" and ctx.llm and ctx.llm.configured:
        try:
            async with typing(update, context):
                intent = await classify(ctx.llm, text, ctx.local_now, ctx.prefs.default_time,
                                        user_id=ctx.user_id, deadline_seconds=ctx.interactive_deadline)
        except LLMError as exc:
            await reply_html(update, esc(exc.user_message) + "\n\n" + esc(agent.EXAMPLES))
            return
    async with typing(update, context):
        reply = await agent.execute(intent, ctx)
    if intent.source == "model" and intent.action not in ("unknown", "help"):
        reply.html += "\n<i>(interpreted by AI — use Undo/Edit if that's wrong)</i>"
    await send(update, context, reply)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def _args(context: ContextTypes.DEFAULT_TYPE) -> str:
    return sanitize_user_text(" ".join(context.args or []))


async def cmd_today(update, context):
    await _run(update, context, Intent("briefing_today"))


async def cmd_tomorrow(update, context):
    await _run(update, context, Intent("plan_tomorrow"))


async def cmd_tasks(update, context):
    await _run(update, context, Intent("list_overdue" if _args(context).lower() == "overdue" else "list_tasks"))


async def cmd_addtask(update, context):
    text = _args(context)
    if not text:
        await reply_html(update, "Usage: <code>/addtask renew passport by 20 Nov urgent</code>")
        return
    ctx = _ctx(update, context)
    intent = parse(f"add task {text}", ctx.local_now, ctx.prefs.default_time)
    await send(update, context, await agent.execute(intent, ctx))


async def cmd_done(update, context):
    arg = _args(context).lstrip("#")
    if not arg.isdigit():
        await reply_html(update, "Usage: <code>/done 12</code> (see /tasks for numbers)")
        return
    await _run(update, context, Intent("complete_task", task_id=int(arg)))


async def cmd_remind(update, context):
    text = _args(context)
    if not text:
        await _run(update, context, Intent("list_reminders"))
        return
    ctx = _ctx(update, context)
    intent = parse(f"remind me {text}", ctx.local_now, ctx.prefs.default_time)
    await send(update, context, await agent.execute(intent, ctx))


async def cmd_reminders(update, context):
    await _run(update, context, Intent("list_reminders"))


async def cmd_followups(update, context):
    await _run(update, context, Intent("list_followups"))


async def cmd_followup(update, context):
    text = _args(context)
    if not text:
        await reply_html(update, "Usage: <code>/followup Dr Ade about the reference letter on friday</code>")
        return
    ctx = _ctx(update, context)
    intent = parse(f"follow up with {text}", ctx.local_now, ctx.prefs.default_time)
    await send(update, context, await agent.execute(intent, ctx))


async def cmd_weekly(update, context):
    ctx = _ctx(update, context)
    async with typing(update, context):
        await send(update, context, await agent.weekly_reply(ctx))


async def cmd_memory(update, context):
    args = context.args or []
    if len(args) >= 3 and args[0].lower() == "edit" and args[1].lstrip("#").isdigit():
        ctx = _ctx(update, context)
        await send(update, context, agent.apply_edit(ctx, "mem", int(args[1].lstrip("#")),
                                                     sanitize_user_text(" ".join(args[2:]))))
        return
    await _run(update, context, Intent("list_memory"))


async def cmd_remember(update, context):
    text = _args(context)
    if not text:
        await reply_html(update, "Usage: <code>/remember I'm preparing for AWS Security Specialty in Q1</code>")
        return
    await _run(update, context, Intent("remember", text=text))


async def cmd_forget(update, context):
    arg = _args(context).lower().lstrip("#")
    ctx = _ctx(update, context)
    if not arg:
        await reply_html(update, "Usage: <code>/forget 3</code>, <code>/forget study</code> or <code>/forget all</code>")
        return
    if arg == "all":
        n = len(store.list_memory(ctx.user_id))
        await send(update, context, agent.Reply(
            f"Delete all {n} memory item(s)? This can't be undone.",
            [[("🗑 Yes, delete all", "cos:forgetall:mem:0"), ("Keep", "cos:noop:mem:0")]]))
        return
    try:
        if arg.isdigit():
            store.forget_memory(ctx.user_id, memory_id=int(arg))
            await reply_html(update, f"🗑 Deleted memory #{arg}.")
            return
        n = store.forget_memory(ctx.user_id, category=MemoryCategory(arg))
        await reply_html(update, f"🗑 Deleted {n} item(s) in <b>{esc(arg)}</b>.")
    except store.NotFound:
        await reply_html(update, f"No memory #{esc(arg)}.")
    except ValueError:
        cats = ", ".join(c.value for c in MemoryCategory)
        await reply_html(update, f"Unknown category. Options: {esc(cats)}")


async def cmd_export(update, context):
    ctx = _ctx(update, context)
    data = agent.export_json(ctx.user_id)
    audit(AuditEvent.DATA_CHANGED, update.effective_user.id if update.effective_user else None,
          entity="export", bytes=len(data))
    name = f"personal-ai-ops-export-{ctx.local_now:%Y%m%d}.json"
    await send(update, context, agent.Reply(
        "Your data as JSON: tasks, reminders, follow-ups, memory, projects, settings and decision log. "
        "No credentials are included.", document=(name, data)))


async def cmd_settings(update, context):
    ctx = _ctx(update, context)
    args = context.args or []
    if len(args) >= 2:
        key = SETTING_ALIASES.get(args[0].lower(), args[0].lower())
        value = " ".join(args[1:])
        try:
            if value.lower() == "default":
                reset_pref(ctx.user_id, key)
                normalised = DEFAULTS[key]
            else:
                normalised = set_pref(ctx.user_id, key, value)
        except (PreferenceError, KeyError) as exc:
            await reply_html(update, esc(str(exc)))
            return
        audit(AuditEvent.DATA_CHANGED, None, entity="preference", key=key)
        await reply_html(update, f"Saved: <b>{esc(key)}</b> = {esc(normalised)}")
        return
    raw = ctx.prefs.raw
    lines = [f"<b>{esc(k)}</b> = {esc(raw[k])}\n<i>{esc(DESCRIPTIONS[k])}</i>" for k in DEFAULTS]
    await reply_html(update, "<b>Settings</b>\n\n" + "\n\n".join(lines) +
                     "\n\nChange: <code>/settings briefing 06:45</code>, <code>/settings quiet 23:00-06:30</code>, "
                     "<code>/settings work mon-fri 08:30-17:30</code>. Reset: <code>/settings quiet default</code>")


async def cmd_cancel(update, context):
    had = context.user_data.pop(PENDING_KEY, None)
    await reply_html(update, "Cancelled." if had else "Nothing to cancel.")


async def cmd_research_stub(update, context):
    await reply_html(update, esc(agent.RESEARCH_MSG))


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    assert q
    await q.answer()
    parts = (q.data or "").split(":")
    if len(parts) < 4 or parts[0] != "cos" or not parts[3].isdigit():
        return
    op, kind, item_id = parts[1], parts[2], int(parts[3])
    arg = parts[4] if len(parts) > 4 else None
    if op == "noop":
        await q.edit_message_reply_markup(None)
        return
    ctx = _ctx(update, context)
    reply = agent.button_action(ctx, op, kind, item_id, arg)
    if op not in ("edit",):
        try:  # buttons are single-use: remove them from the original message
            await q.edit_message_reply_markup(None)
        except Exception:
            logger.debug("Could not clear buttons", exc_info=True)
    await send(update, context, reply)


COMMANDS = [
    ("today", "Daily briefing", cmd_today),
    ("tomorrow", "Plan for tomorrow", cmd_tomorrow),
    ("tasks", "Open tasks (/tasks overdue)", cmd_tasks),
    ("addtask", "Add a task", cmd_addtask),
    ("done", "Mark a task done", cmd_done),
    ("remind", "Set a reminder", cmd_remind),
    ("reminders", "Pending reminders", cmd_reminders),
    ("followups", "Pending follow-ups", cmd_followups),
    ("followup", "Track a follow-up", cmd_followup),
    ("weekly", "Weekly review", cmd_weekly),
    ("memory", "What I remember about you", cmd_memory),
    ("remember", "Store a fact", cmd_remember),
    ("forget", "Delete stored facts", cmd_forget),
    ("export", "Download all your data", cmd_export),
    ("settings", "Briefing time, quiet hours…", cmd_settings),
    ("cancel", "Cancel a pending question", cmd_cancel),
]
RESEARCH_STUBS = ["research", "opportunities", "deadlines", "sources"]


def register(application: Application) -> None:
    for name, _, fn in COMMANDS:
        application.add_handler(CommandHandler(name, fn))
    for name in RESEARCH_STUBS:
        application.add_handler(CommandHandler(name, cmd_research_stub))
    application.add_handler(CallbackQueryHandler(on_button, pattern=r"^cos:"))
