"""Audit logging for security-relevant events.

Never put secrets or full message bodies in audit details; record what
happened, not the user's private content.
"""

from __future__ import annotations

import logging
from typing import Any

from app.database.models import AuditLog
from app.database.session import session_scope

logger = logging.getLogger("audit")


class AuditEvent:
    ACCESS_DENIED = "access_denied"
    RATE_LIMITED = "rate_limited"
    BOT_STARTED = "bot_started"
    USER_REGISTERED = "user_registered"
    HANDLER_ERROR = "handler_error"
    DATA_CHANGED = "data_changed"
    DATA_DELETED = "data_deleted"
    EXTERNAL_ACTION_APPROVED = "external_action_approved"
    PROMPT_INJECTION_SUSPECTED = "prompt_injection_suspected"
    URL_REJECTED = "url_rejected"
    JOB_FAILED = "job_failed"


def audit(event_type: str, actor_telegram_id: int | None = None, **details: Any) -> None:
    """Best-effort audit write; failures are logged but never raised."""
    try:
        with session_scope() as s:
            s.add(AuditLog(event_type=event_type, actor_telegram_id=actor_telegram_id, details=details))
        logger.info("audit event=%s actor=%s", event_type, actor_telegram_id)
    except Exception:
        logger.exception("Failed to write audit event %s", event_type)
