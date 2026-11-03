"""Milestone 1 handlers: /start, /help, /status, /ping, /aitest and fallback."""

from __future__ import annotations

import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.bot.formatting import esc
from app.core.config import Settings
from app.core.llm import GeminiClient, LLMError
from app.core.timeutils import format_local, utcnow
from app.services.health import collect_health
from app.services.users import get_or_create_user

HELP_TEXT = """<b>Personal AI Ops</b>

<b>Available now</b>
/start – register and initialise
/help – this message
/status – system health (database, AI, jobs)
/ping – connectivity check
/aitest – one small Gemini call to verify the API key

<b>Coming next</b>
Milestone 2 – Chief of Staff: /today /tasks /addtask /remind /followups /weekly /memory /settings
Milestone 3 – Research: /research /opportunities /deadlines /sources

Natural-language requests ("remind me to…") are enabled with Milestone 2."""


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    assert u and update.effective_message
    get_or_create_user(u.id, update.effective_chat.id if update.effective_chat else None, u.full_name)
    await update.effective_message.reply_text(
        f"Initialised. Local time: <b>{esc(format_local(utcnow()))}</b>.\n\nSend /help to see what's available.",
        parse_mode=ParseMode.HTML,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message
    await update.effective_message.reply_text(f"pong · {format_local(utcnow())}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message
    settings = _settings(context)
    h = collect_health(settings)
    ok, bad = "✅", "❌"
    jobs = context.application.job_queue.jobs() if context.application.job_queue else ()
    job_lines = "\n".join(
        f"• {esc(j.name)} – next {esc(format_local(j.next_t)) if j.next_t else '—'}" for j in jobs
    ) or "• none"
    usage = "—" if h.llm_requests_today is None else f"{h.llm_requests_today}/{h.llm_daily_budget}"
    text = (
        "<b>System status</b>\n"
        f"{ok if h.database_ok else bad} Database\n"
        f"{ok if h.schema_ok else bad} Schema {esc(h.schema_current or 'none')} (head {esc(h.schema_head or '?')})\n"
        f"{ok if h.gemini_configured else bad} Gemini key configured\n"
        f"Models: {esc(h.gemini_model)} / light: {esc(h.gemini_model_light)}\n"
        f"AI requests today: {esc(usage)}\n"
        f"Timezone: {esc(settings.timezone)} · now {esc(format_local(utcnow()))}\n\n"
        f"<b>Scheduled jobs</b>\n{job_lines}"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def aitest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_user
    llm: GeminiClient = context.application.bot_data["llm"]
    started = time.monotonic()
    try:
        result = await llm.generate(
            "Reply with exactly the word: OK",
            tier="light", temperature=0, max_output_tokens=256,
            agent="system", action="aitest",
        )
    except LLMError as exc:
        await update.effective_message.reply_text(exc.user_message)
        return
    elapsed = int((time.monotonic() - started) * 1000)
    await update.effective_message.reply_text(
        f"Gemini reachable.\nModel: {result.model}\nReply: {result.text.strip()[:50]}\n"
        f"Tokens: {result.total_tokens} · {elapsed} ms"
    )


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message
    await update.effective_message.reply_text(
        "Message received. Natural-language routing to the agents is not built yet "
        "(Milestone 2). Use /help for available commands."
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message
    await update.effective_message.reply_text("Unknown or not-yet-available command. See /help.")


BOT_COMMANDS = [
    ("start", "Initialise the bot"),
    ("help", "Show capabilities"),
    ("status", "System health"),
    ("ping", "Connectivity check"),
    ("aitest", "Verify the Gemini API key"),
]


def register(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("aitest", aitest))


def register_fallbacks(application: Application) -> None:
    """Must be registered last: within a group, the first matching handler wins."""
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))
