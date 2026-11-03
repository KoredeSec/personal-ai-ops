"""SQLite backup and restore.

Uses SQLite's online backup API, which is safe while the bot is running
(WAL mode) and produces a consistent snapshot. Every backup is checked with
`PRAGMA integrity_check` before it is kept. For PostgreSQL use `pg_dump`.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings

logger = logging.getLogger(__name__)

BACKUP_GLOB = "app-*.db"


class BackupError(RuntimeError):
    pass


def sqlite_path(database_url: str) -> Path:
    if not database_url.startswith("sqlite:///") or ":memory:" in database_url:
        raise BackupError("Built-in backups support file-based SQLite only; use pg_dump for PostgreSQL")
    return Path(database_url.removeprefix("sqlite:///"))


def _integrity_ok(path: Path) -> bool:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def list_backups(backup_dir: Path) -> list[Path]:
    """Newest first."""
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob(BACKUP_GLOB), reverse=True)


def create_backup(settings: Settings) -> Path:
    src_path = sqlite_path(settings.database_url)
    if not src_path.exists():
        raise BackupError(f"Database not found: {src_path}")
    backup_dir = settings.backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = backup_dir / f"app-{stamp}.db"
    tmp = dest.with_suffix(".tmp")

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    if not _integrity_ok(tmp):
        tmp.unlink(missing_ok=True)
        raise BackupError("Backup failed integrity check")
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)

    for old in list_backups(backup_dir)[settings.backup_retention_count:]:
        old.unlink(missing_ok=True)
    logger.info("Backup written: %s (%d bytes)", dest.name, dest.stat().st_size)
    return dest


def restore_backup(settings: Settings, backup: Path) -> Path:
    """Replace the live database with `backup`. STOP THE BOT FIRST.

    The current database is copied aside first and the path of that safety
    copy is returned, so a restore can itself be undone.
    """
    if not backup.exists():
        raise BackupError(f"Backup not found: {backup}")
    if not _integrity_ok(backup):
        raise BackupError("Backup failed integrity check; refusing to restore")
    target = sqlite_path(settings.database_url)
    safety = target.with_name(f"{target.name}.pre-restore-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
    if target.exists():
        shutil.copy2(target, safety)
        for suffix in ("-wal", "-shm"):  # stale WAL must not be replayed onto restored data
            Path(f"{target}{suffix}").unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    if not _integrity_ok(target):
        raise BackupError(f"Restored database failed integrity check; safety copy at {safety}")
    logger.info("Restored %s from %s (previous copy: %s)", target, backup.name, safety.name)
    return safety


def latest_backup_time(settings: Settings) -> datetime | None:
    backups = list_backups(settings.backup_dir)
    if not backups:
        return None
    return datetime.fromtimestamp(backups[0].stat().st_mtime, UTC)
