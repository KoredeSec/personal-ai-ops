"""User registration and lookup (deterministic, no LLM)."""

from __future__ import annotations

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import User
from app.database.session import session_scope
from app.services.audit import AuditEvent, audit


def get_or_create_user(telegram_user_id: int, chat_id: int | None, display_name: str | None) -> User:
    """Upsert the (already authorised) Telegram user. Returns a detached User."""
    with session_scope() as s:
        user = s.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        created = user is None
        if created:
            user = User(
                telegram_user_id=telegram_user_id,
                telegram_chat_id=chat_id,
                display_name=display_name,
                timezone=get_settings().timezone,
            )
            s.add(user)
        else:
            if chat_id and user.telegram_chat_id != chat_id:
                user.telegram_chat_id = chat_id
            if display_name and user.display_name != display_name:
                user.display_name = display_name
        s.flush()
        s.expunge(user)
    if created:
        audit(AuditEvent.USER_REGISTERED, telegram_user_id)
    return user


def get_user_by_telegram_id(telegram_user_id: int) -> User | None:
    with session_scope() as s:
        user = s.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if user is not None:
            s.expunge(user)
        return user
