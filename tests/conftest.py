import os

# Must be set before any app module reads settings.
os.environ.setdefault("APP_ENV", "test")
os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:TEST_TOKEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "1001"
os.environ["GEMINI_API_KEY"] = "AIzaTEST_KEY_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
os.environ["TIMEZONE"] = "Africa/Lagos"

import pytest  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.database import session as db_session  # noqa: E402


def _clear_caches() -> None:
    get_settings.cache_clear()
    db_session.get_sessionmaker.cache_clear()
    if db_session.get_engine.cache_info().currsize:
        db_session.get_engine().dispose()
    db_session.get_engine.cache_clear()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh SQLite database migrated to head via Alembic (not create_all)."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "heartbeat"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    _clear_caches()
    from app.database.migrate import upgrade_to_head

    upgrade_to_head(url)
    yield url
    _clear_caches()


@pytest.fixture()
def settings(db):
    return get_settings()
