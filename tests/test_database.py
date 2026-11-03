from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from app.database.base import Base
from app.database.migrate import alembic_config, schema_status, upgrade_to_head
from app.database.models import NotificationLog, Priority, Task, TaskStatus, User
from app.database.session import build_engine, session_scope

EXPECTED_TABLES = {
    "users", "preferences", "memory_items", "projects", "tasks", "reminders", "followups",
    "research_sources", "research_articles", "opportunities", "opportunity_deadlines",
    "conversation_summaries", "agent_execution_logs", "audit_logs", "llm_cache",
    "notification_log", "decision_events",
}


def test_migration_creates_all_tables(db):
    tables = set(inspect(build_engine(db)).get_table_names())
    assert EXPECTED_TABLES <= tables
    current, head = schema_status(db)
    assert current == head == "0003_decision_events"


def test_migrations_match_models_exactly(db):
    """Guards against model changes without a migration."""
    engine = build_engine(db)
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diffs == []


def test_downgrade_and_reupgrade(db):
    command.downgrade(alembic_config(db), "base")
    assert set(inspect(build_engine(db)).get_table_names()) <= {"alembic_version"}
    upgrade_to_head(db)
    assert EXPECTED_TABLES <= set(inspect(build_engine(db)).get_table_names())


def _user(s):
    u = User(telegram_user_id=1001, display_name="Me")
    s.add(u)
    s.flush()
    return u


def test_utc_datetime_roundtrip_from_other_timezone(db):
    lagos_9am = datetime(2026, 11, 4, 9, 0, tzinfo=timezone(timedelta(hours=1)))
    with session_scope() as s:
        u = _user(s)
        s.add(Task(user_id=u.id, title="t", due_at=lagos_9am))
    with session_scope() as s:
        t = s.scalar(select(Task))
        assert t.due_at == datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
        assert t.due_at.tzinfo is not None
        assert t.priority == Priority.MEDIUM and t.status == TaskStatus.OPEN


def test_naive_datetime_rejected(db):
    with pytest.raises(StatementError):
        with session_scope() as s:
            u = _user(s)
            s.add(Task(user_id=u.id, title="t", due_at=datetime(2026, 1, 1)))


def test_invalid_enum_value_rejected(db):
    with pytest.raises(StatementError):
        with session_scope() as s:
            u = _user(s)
            s.add(Task(user_id=u.id, title="t", priority="extreme"))


def test_notification_dedupe_key_unique(db):
    with session_scope() as s:
        s.add(NotificationLog(dedupe_key="reminder:1", kind="reminder"))
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(NotificationLog(dedupe_key="reminder:1", kind="reminder"))


def test_foreign_keys_enforced_and_cascade(db):
    with session_scope() as s:
        u = _user(s)
        s.add(Task(user_id=u.id, title="t"))
    with session_scope() as s:
        s.delete(s.scalar(select(User)))
    with session_scope() as s:
        assert s.scalar(select(Task)) is None
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(Task(user_id=999, title="orphan"))


def test_session_scope_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with session_scope() as s:
            _user(s)
            raise RuntimeError("boom")
    with session_scope() as s:
        assert s.scalar(select(User)) is None
