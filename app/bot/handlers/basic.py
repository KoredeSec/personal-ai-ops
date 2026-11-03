"""Core handlers: /start, /help, /status, /ping, /aitest and fallbacks."""

from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.bot.formatting import esc
from app.bot.reply import reply_html, reply_text, typing
from app.core.config import Settings
from app.core.llm import GeminiClient, LLMError
from app.core.timeutils import format_local, utcnow
from app.services.health import collect_health
from app.services.users import get_or_create_user

# Lists only what works. New commands are added here as they ship.
HELP_TEXT = """<b>Personal AI Ops</b>

/status – system health: database, AI, backups, jobs
/ping – check the bot is responding
/aitest – verify the Gemini connection (uses 1 AI request)
/help – this message

Times are shown in Africa/Lagos time (WAT)."""


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    assert u
    get_or_create_user(u.id, update.effective_chat.id if update.effective_chat else None, u.full_name)
    await reply_html(
        update,
        f"Ready. Local time: <b>{esc(format_local(utcnow()))}</b>.\nSend /help to see what I can do.",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_html(update, HELP_TEXT)


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text(update, f"pong · {format_local(utcnow())}")


def _line(ok: bool, label: str, detail: str = "") -> str:
    mark = "✅ OK" if ok else "❌ FAILED"
    return f"{mark} – {esc(label)}{(': ' + esc(detail)) if detail else ''}"


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    h = collect_health(settings)
    problems = h.problems()
    summary = ("<b>All systems OK</b>" if not problems
               else f"<b>{len(problems)} problem(s):</b>\n" + "\n".join(f"• {esc(p)}" for p in problems))

    jobs = context.application.job_queue.jobs() if context.application.job_queue else ()
    job_lines = "\n".join(
        f"• {esc(j.name)} – next {esc(format_local(j.next_t)) if j.next_t else '—'}" for j in jobs
    ) or "• none"
    usage = "—" if h.llm_requests_today is None else f"{h.llm_requests_today}/{h.llm_daily_budget}"
    hb = "never" if h.heartbeat_age is None else f"{h.heartbeat_age:.0f}s ago"
    backup = format_local(h.last_backup) if h.last_backup else ("n/a" if not h.backups_expected else "never")
    text = (
        f"{summary}\n\n<b>Details</b>\n"
        f"{_line(h.database_ok, 'Database')}\n"
        f"{_line(h.schema_ok, 'Schema', f'{h.schema_current or 'none'} (expected {h.schema_head or '?'})')}\n"
        f"{_line(h.gemini_configured, 'Gemini key')}\n"
        f"{_line(h.heartbeat_ok, 'Heartbeat', hb)}\n"
        f"{_line(h.backup_ok, 'Last backup', backup)}\n"
        f"Models: {esc(h.gemini_model)} / light: {esc(h.gemini_model_light)}\n"
        f"AI requests today: {esc(usage)}\n"
        f"Now: {esc(format_local(utcnow()))}\n\n"
        f"<b>Scheduled jobs</b>\n{job_lines}"
    )
    await reply_html(update, text)


async def aitest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    llm: GeminiClient = context.application.bot_data["llm"]
    try:
        async with typing(update, context):
            result = await llm.generate(
                "Reply with exactly the word: OK",
                tier="light", temperature=0, max_output_tokens=256,
                agent="system", action="aitest",
                deadline_seconds=settings.gemini_interactive_deadline_seconds,
            )
    except LLMError as exc:
        await reply_text(update, exc.user_message)
        return
    await reply_text(
        update,
        f"Gemini is working.\nModel: {result.model}\nReply: {result.text.strip()[:50]}\n"
        f"Tokens: {result.total_tokens} · {result.latency_ms} ms",
    )


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text(update, "I can't act on free-text messages yet. Send /help to see the commands I understand.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text(update, "I don't know that command. Send /help for the list.")


BOT_COMMANDS = [
    ("status", "System health"),
    ("ping", "Check the bot is responding"),
    ("aitest", "Verify the Gemini connection"),
    ("help", "Show capabilities"),
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
