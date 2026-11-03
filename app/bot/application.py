"""Builds the Telegram Application: gates, handlers, error handler, jobs."""

from __future__ import annotations

import logging
import uuid

from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, Defaults
from telegram.request import BaseRequest

from app.bot.handlers import basic
from app.bot.middleware import install_gates
from app.core.config import Settings
from app.core.llm import GeminiClient
from app.core.scheduler import default_jobs, register_jobs
from app.services.audit import AuditEvent, audit
from app.services.llm_store import DatabaseUsageStore

logger = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log full details server-side; show the user only a reference id."""
    ref = uuid.uuid4().hex[:8]
    logger.error("Unhandled error ref=%s", ref, exc_info=context.error)
    actor = update.effective_user.id if isinstance(update, Update) and update.effective_user else None
    audit(AuditEvent.HANDLER_ERROR, actor, ref=ref, error_type=type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(f"Something went wrong (ref {ref}). It has been logged.")
        except Exception:
            logger.exception("Failed to send error message ref=%s", ref)


async def _post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    me = await application.bot.get_me()
    logger.info("Connected to Telegram as @%s (id=%s)", me.username, me.id)
    await application.bot.set_my_commands([BotCommand(c, d) for c, d in basic.BOT_COMMANDS])
    register_jobs(application, default_jobs())
    audit(AuditEvent.BOT_STARTED, None, bot_username=me.username,
          allowed_users=len(settings.telegram_allowed_user_ids))


def build_application(
    settings: Settings,
    request: BaseRequest | None = None,
    llm: GeminiClient | None = None,
) -> Application:
    """`request` / `llm` are injection points for offline tests."""
    assert settings.telegram_bot_token is not None
    builder = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token.get_secret_value())
        .defaults(Defaults(tzinfo=settings.tz))
        .concurrent_updates(False)  # single user: process in order, simpler state
        .post_init(_post_init)
    )
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    else:
        builder = builder.connect_timeout(10).read_timeout(30).write_timeout(30)
    application = builder.build()
    application.bot_data["settings"] = settings
    application.bot_data["llm"] = llm or GeminiClient(settings, store=DatabaseUsageStore())

    install_gates(application, settings)
    basic.register(application)
    # Milestone 2/3 handler modules register here, before the fallbacks.
    basic.register_fallbacks(application)
    application.add_error_handler(error_handler)
    return application


ALLOWED_UPDATES = [Update.MESSAGE, Update.EDITED_MESSAGE, Update.CALLBACK_QUERY]
