"""Timezone helpers.

Convention: everything is *stored* in UTC and *displayed* in the configured
local timezone (Africa/Lagos by default). Never store naive local times.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def local_tz() -> ZoneInfo:
    return get_settings().tz


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_local() -> datetime:
    return datetime.now(local_tz())


def to_utc(dt: datetime) -> datetime:
    """Convert to aware UTC. Naive datetimes are interpreted as *local* time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(UTC)


def to_local(dt: datetime) -> datetime:
    """Convert to aware local time. Naive datetimes are interpreted as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(local_tz())


def local_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a local calendar day, expressed in UTC."""
    tz = local_tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def format_local(dt: datetime | None, with_time: bool = True) -> str:
    """Unambiguous human format, e.g. 'Tue 04 Nov 2026, 09:00 WAT'."""
    if dt is None:
        return "—"
    local = to_local(dt)
    if with_time:
        return local.strftime("%a %d %b %Y, %H:%M %Z")
    return local.strftime("%a %d %b %Y")
