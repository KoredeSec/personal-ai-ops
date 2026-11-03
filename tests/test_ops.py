"""Backups, heartbeat/watchdog, healthcheck, audit throttle, job alerts, migration backfill."""

import sqlite3
import threading
import time

import pytest
from alembic import command
from sqlalchemy import select, text

from app.bot.middleware import DenialThrottle
from app.core.config import Settings
from app.database.migrate import alembic_config, upgrade_to_head
from app.database.models import NotificationLog, User
from app.database.session import build_engine, session_scope
from app.services import backup
from app.services.heartbeat import heartbeat_age_seconds, start_watchdog, write_heartbeat


# --------------------------------------------------------------------------
# Backup / restore
# --------------------------------------------------------------------------
def _add_user(tg_id):
    with session_scope() as s:
        s.add(User(telegram_user_id=tg_id))


def _user_ids():
    with session_scope() as s:
        return sorted(s.scalars(select(User.telegram_user_id)).all())


def test_backup_is_consistent_private_and_pruned(settings):
    settings.backup_retention_count = 2
    _add_user(1)
    paths = [backup.create_backup(settings) for _ in range(3)]
    kept = backup.list_backups(settings.backup_dir)
    assert kept == sorted(paths, reverse=True)[:2]  # oldest pruned
    assert oct(kept[0].stat().st_mode & 0o777) == "0o600"
    conn = sqlite3.connect(kept[0])
    assert conn.execute("SELECT telegram_user_id FROM users").fetchall() == [(1,)]
    conn.close()
    assert backup.latest_backup_time(settings) is not None


def test_restore_roundtrip_keeps_safety_copy(settings):
    _add_user(1)
    snap = backup.create_backup(settings)
    _add_user(2)
    assert _user_ids() == [1, 2]
    from app.database.session import get_engine
    get_engine().dispose()  # equivalent of stopping the bot
    safety = backup.restore_backup(settings, snap)
    assert _user_ids() == [1]
    assert safety.exists()


def test_corrupt_backup_refused(settings):
    bad = settings.backup_dir / "app-bad.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
    with pytest.raises((backup.BackupError, sqlite3.DatabaseError)):
        backup.restore_backup(settings, bad)
    assert _user_ids() == []  # live DB untouched


def test_postgres_url_rejected_for_builtin_backup():
    s = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/db")
    with pytest.raises(backup.BackupError):
        backup.create_backup(s)


# --------------------------------------------------------------------------
# Heartbeat, watchdog, healthcheck
# --------------------------------------------------------------------------
def test_heartbeat_roundtrip(tmp_path):
    p = tmp_path / "hb"
    assert heartbeat_age_seconds(p) is None
    write_heartbeat(p)
    assert heartbeat_age_seconds(p) < 2


def test_watchdog_fires_on_stale_heartbeat(tmp_path):
    fired = threading.Event()
    start_watchdog(tmp_path / "missing", max_age_seconds=1, check_every=0.01, grace_seconds=0,
                   on_stale=lambda age: fired.set())
    assert fired.wait(2)


def test_watchdog_quiet_while_heartbeat_fresh(tmp_path):
    p = tmp_path / "hb"
    fired = threading.Event()
    stop = time.monotonic() + 0.3

    def beat():
        while time.monotonic() < stop:
            write_heartbeat(p)
            time.sleep(0.02)

    write_heartbeat(p)
    t = threading.Thread(target=beat)
    t.start()
    start_watchdog(p, max_age_seconds=5, check_every=0.02, grace_seconds=0, on_stale=lambda a: fired.set())
    t.join()
    assert not fired.is_set()


def test_healthcheck_script(settings, capsys):
    from scripts import healthcheck

    assert healthcheck.main() == 1 and "no heartbeat" in capsys.readouterr().out
    write_heartbeat(settings.heartbeat_file)
    assert healthcheck.main() == 0


# --------------------------------------------------------------------------
# Audit throttle
# --------------------------------------------------------------------------
def test_denial_throttle_one_audit_per_interval_with_suppressed_count():
    t = [0.0]
    th = DenialThrottle(interval=3600, clock=lambda: t[0])
    assert th.check(666) == (True, 0)
    assert th.check(666) == (False, 0)
    assert th.check(666) == (False, 0)
    assert th.check(777) == (True, 0)  # per stranger
    t[0] = 3601
    assert th.check(666) == (True, 2)  # reports what was suppressed


def test_denial_throttle_memory_bounded():
    th = DenialThrottle(max_tracked=10)
    for i in range(100):
        th.check(i)
    assert len(th._state) <= 10


# --------------------------------------------------------------------------
# Job failure alerts
# --------------------------------------------------------------------------
async def test_job_failure_alerts_owner_once_per_day(settings):
    from tests.test_bot import FakeTelegram, StubLLM

    from app.bot.application import build_application
    from app.bot.jobs import on_job_error

    fake = FakeTelegram()
    app = build_application(settings, request=fake, llm=StubLLM(settings))
    await app.initialize()
    try:
        class Ctx:
            application = app

        for _ in range(3):
            await on_job_error("backup", RuntimeError("disk full /secret/path"), Ctx())
    finally:
        await app.shutdown()
    alerts = fake.sent_texts()
    assert len(alerts) == 1 and "backup" in alerts[0]
    assert "/secret/path" not in alerts[0]  # details stay in server logs


# --------------------------------------------------------------------------
# Migration 0002 on a database that already has data
# --------------------------------------------------------------------------
def test_migration_0002_backfills_existing_rows(db):
    cfg = alembic_config(db)
    command.downgrade(cfg, "0001_initial")
    engine = build_engine(db)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO notification_log (dedupe_key, kind, created_at) "
                          "VALUES ('old:1', 'reminder', '2026-01-01 00:00:00')"))
    engine.dispose()
    upgrade_to_head(db)
    with session_scope() as s:
        r = s.scalar(select(NotificationLog))
        assert (r.status.value, r.attempts) == ("sent", 1)
        assert r.updated_at == r.created_at
