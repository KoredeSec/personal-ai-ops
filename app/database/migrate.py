"""Programmatic migration helpers used by the app, scripts and tests."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.core.config import PROJECT_ROOT, get_settings
from app.database.session import build_engine


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url or get_settings().database_url
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade_to_head(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), "head")


def schema_status(database_url: str | None = None) -> tuple[str | None, str | None]:
    """Return (current_db_revision, head_revision)."""
    url = database_url or get_settings().database_url
    cfg = alembic_config(url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    engine = build_engine(url)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    return current, head
