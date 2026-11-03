"""Pre-handler gates. They run in negative handler groups, before any command.

group -2: authorisation (allowlist + private chats only). Unauthorised updates
          are dropped silently so the bot does not reveal that it exists.
group -1: per-user rate limiting.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ApplicationHandlerStop, ContextTypes, TypeHandler

from app.core.config import Settings
from app.core.security import SlidingWindowRateLimiter, is_authorised
from app.services.audit import AuditEvent, audit

logger = logging.getLogger(__name__)


def install_gates(application: Application, settings: Settings) -> None:
    allowed = frozenset(settings.telegram_allowed_user_ids)
    limiter = SlidingWindowRateLimiter(settings.rate_limit_per_minute, 60.0)
    warned: set[int] = set()

    async def auth_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not isinstance(update, Update):
            raise ApplicationHandlerStop
        user = update.effective_user
        chat = update.effective_chat
        user_id = user.id if user else None
        if not is_authorised(user_id, allowed):
            logger.warning("Dropped update from unauthorised user_id=%s", user_id)
            audit(AuditEvent.ACCESS_DENIED, user_id, chat_type=chat.type if chat else None)
            raise ApplicationHandlerStop
        if chat is None or chat.type != ChatType.PRIVATE:
            # Personal data must never be posted into groups/channels.
            audit(AuditEvent.ACCESS_DENIED, user_id, reason="non_private_chat",
                  chat_type=chat.type if chat else None)
            raise ApplicationHandlerStop

    async def rate_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert isinstance(update, Update) and update.effective_user
        uid = update.effective_user.id
        if limiter.allow(uid):
            warned.discard(uid)
            return
        if uid not in warned:  # warn once per burst, then stay quiet
            warned.add(uid)
            audit(AuditEvent.RATE_LIMITED, uid)
            if update.effective_message:
                wait = int(limiter.retry_after(uid)) + 1
                await update.effective_message.reply_text(f"Rate limit reached. Try again in ~{wait}s.")
        raise ApplicationHandlerStop

    application.add_handler(TypeHandler(object, auth_gate), group=-2)
    application.add_handler(TypeHandler(Update, rate_gate), group=-1)
