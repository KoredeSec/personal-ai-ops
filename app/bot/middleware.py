"""Pre-handler gates. They run in negative handler groups, before any command.

group -2: authorisation (allowlist + private chats only). Unauthorised updates
          are dropped silently so the bot does not reveal that it exists.
          Audit writes for strangers are throttled so a flood cannot grow the
          audit table without bound.
group -1: per-user rate limiting.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ApplicationHandlerStop, ContextTypes, TypeHandler

from app.bot.reply import reply_text
from app.core.config import Settings
from app.core.security import SlidingWindowRateLimiter, is_authorised
from app.core.timeutils import format_local, utcnow
from app.services.audit import AuditEvent, audit

logger = logging.getLogger(__name__)

DENIAL_AUDIT_INTERVAL = 3600.0  # at most one audit row per stranger per hour
MAX_TRACKED_STRANGERS = 1000


class DenialThrottle:
    """Decides whether an access denial should be written to the audit log."""

    def __init__(self, interval: float = DENIAL_AUDIT_INTERVAL,
                 clock: Callable[[], float] = time.monotonic, max_tracked: int = MAX_TRACKED_STRANGERS):
        self.interval = interval
        self._clock = clock
        self._max = max_tracked
        self._state: dict[int | None, tuple[float, int]] = {}  # id -> (last_audit, suppressed)

    def check(self, actor: int | None) -> tuple[bool, int]:
        """Returns (should_audit, suppressed_since_last_audit)."""
        now = self._clock()
        last = self._state.get(actor)
        if last is not None and now - last[0] < self.interval:
            self._state[actor] = (last[0], last[1] + 1)
            return False, 0
        if len(self._state) >= self._max:
            self._state.clear()  # bounded memory under a distributed flood
        suppressed = last[1] if last else 0
        self._state[actor] = (now, 0)
        return True, suppressed


def install_gates(application: Application, settings: Settings) -> None:
    allowed = frozenset(settings.telegram_allowed_user_ids)
    limiter = SlidingWindowRateLimiter(settings.rate_limit_per_minute, 60.0)
    throttle = DenialThrottle()
    warned: set[int] = set()

    def _deny(user_id: int | None, **details) -> None:
        should, suppressed = throttle.check(user_id)
        if should:
            audit(AuditEvent.ACCESS_DENIED, user_id, suppressed_since_last=suppressed, **details)

    async def auth_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not isinstance(update, Update):
            raise ApplicationHandlerStop
        user = update.effective_user
        chat = update.effective_chat
        user_id = user.id if user else None
        if not is_authorised(user_id, allowed):
            logger.warning("Dropped update from unauthorised user_id=%s", user_id)
            _deny(user_id, chat_type=chat.type if chat else None)
            raise ApplicationHandlerStop
        if chat is None or chat.type != ChatType.PRIVATE:
            # Personal data must never be posted into groups/channels.
            _deny(user_id, reason="non_private_chat", chat_type=chat.type if chat else None)
            raise ApplicationHandlerStop

    async def rate_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert isinstance(update, Update) and update.effective_user
        uid = update.effective_user.id
        if limiter.allow(uid):
            warned.discard(uid)
            return
        if uid not in warned:  # warn once per burst, with an exact unblock time
            warned.add(uid)
            audit(AuditEvent.RATE_LIMITED, uid)
            until = utcnow() + timedelta(seconds=limiter.retry_after(uid) + 1)
            await reply_text(
                update,
                f"Too many messages. I'll ignore new messages until {format_local(until)}, "
                "then respond normally. Nothing you sent before this was lost.",
            )
        raise ApplicationHandlerStop

    application.add_handler(TypeHandler(object, auth_gate), group=-2)
    application.add_handler(TypeHandler(Update, rate_gate), group=-1)
