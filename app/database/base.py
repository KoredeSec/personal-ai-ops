"""Declarative base, shared column types and mixins."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# Deterministic constraint names are required for Alembic batch migrations on
# SQLite and make PostgreSQL migrations predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime that is always stored and returned as UTC.

    SQLite has no timezone support, so values are normalised to UTC before
    storage and re-tagged as UTC on load. On PostgreSQL this maps to
    `TIMESTAMP WITH TIME ZONE`. Naive datetimes are rejected to prevent silent
    local/UTC confusion.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime passed to UTCDateTime column; use an aware datetime")
        value = value.astimezone(UTC)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def str_enum(enum_cls: type[enum.Enum], length: int = 32) -> Enum:
    """Portable string-backed enum column.

    Stored as VARCHAR (no native DB enum, no CHECK constraint) so adding values
    later does not require a constraint migration; values are validated in
    Python on write.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        validate_strings=True,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )
