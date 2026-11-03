"""User preferences with validated defaults (deterministic).

Stored as key -> JSON string in the `preferences` table. Defaults live here,
so a missing row always means "use the default", and changing a default in
code changes it for everyone who never customised it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import select

from app.database.models import Preference
from app.database.session import session_scope

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULTS: dict[str, str] = {
    "briefing_time": "07:00",
    "weekly_review": "sun 18:00",
    "quiet_hours": "22:00-07:00",
    "default_time": "09:00",
    "work_hours": "mon-fri 09:00-17:00",
}

DESCRIPTIONS = {
    "briefing_time": "Daily briefing time, e.g. 07:00",
    "weekly_review": "Weekly review day and time, e.g. sun 18:00",
    "quiet_hours": "No system-initiated messages in this window, e.g. 22:00-07:00 (or 'off')",
    "default_time": "Time used when you give a date without a time, e.g. 09:00",
    "work_hours": "Work hours, excluded from 'time for personal development', e.g. mon-fri 09:00-17:00 (or 'off')",
}

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class PreferenceError(ValueError):
    pass


def _hhmm(value: str) -> time:
    m = _HHMM.match(value.strip())
    if not m:
        raise PreferenceError(f"'{value}' isn't a 24-hour time like 07:30")
    return time(int(m.group(1)), int(m.group(2)))


def _range(value: str) -> tuple[time, time]:
    a, sep, b = value.partition("-")
    if not sep:
        raise PreferenceError(f"'{value}' should look like 22:00-07:00")
    start, end = _hhmm(a), _hhmm(b)
    if start == end:
        raise PreferenceError("Start and end can't be the same")
    return start, end


def _days(value: str) -> list[int]:
    value = value.strip().lower()
    if "-" in value:
        a, b = value.split("-", 1)
        if a[:3] not in DAYS or b[:3] not in DAYS:
            raise PreferenceError(f"'{value}' isn't a day range like mon-fri")
        i, j = DAYS.index(a[:3]), DAYS.index(b[:3])
        return list(range(i, j + 1)) if i <= j else list(range(i, 7)) + list(range(0, j + 1))
    out = []
    for part in value.split(","):
        if part.strip()[:3] not in DAYS:
            raise PreferenceError(f"'{part}' isn't a day")
        out.append(DAYS.index(part.strip()[:3]))
    return out


def validate(key: str, value: str) -> str:
    """Return the normalised value or raise PreferenceError."""
    if key not in DEFAULTS:
        raise PreferenceError(f"Unknown setting '{key}'. Options: {', '.join(DEFAULTS)}")
    v = re.sub(r"\s+", " ", value.strip().lower())
    if key in ("briefing_time", "default_time"):
        return _hhmm(v).strftime("%H:%M")
    if key == "weekly_review":
        parts = v.split(" ")
        if len(parts) != 2 or parts[0][:3] not in DAYS:
            raise PreferenceError("Use a day and time, e.g. sun 18:00")
        return f"{parts[0][:3]} {_hhmm(parts[1]).strftime('%H:%M')}"
    if key == "quiet_hours":
        if v == "off":
            return "off"
        s, e = _range(v)
        return f"{s:%H:%M}-{e:%H:%M}"
    if key == "work_hours":
        if v == "off":
            return "off"
        parts = v.split(" ")
        if len(parts) != 2:
            raise PreferenceError("Use days and hours, e.g. mon-fri 09:00-17:00")
        _days(parts[0])
        s, e = _range(parts[1])
        if e <= s:
            raise PreferenceError("Work hours must end after they start on the same day")
        return f"{parts[0]} {s:%H:%M}-{e:%H:%M}"
    raise PreferenceError(key)  # pragma: no cover


@dataclass(frozen=True)
class Prefs:
    briefing_time: time
    weekly_day: int
    weekly_time: time
    quiet: tuple[time, time] | None
    default_time: time
    work_days: list[int]
    work: tuple[time, time] | None
    raw: dict[str, str]

    def in_quiet_hours(self, local: datetime) -> bool:
        if not self.quiet:
            return False
        start, end = self.quiet
        t = local.timetz().replace(tzinfo=None)
        return (start <= t < end) if start < end else (t >= start or t < end)

    def work_window(self, local_date) -> tuple[time, time] | None:
        if not self.work or local_date.weekday() not in self.work_days:
            return None
        return self.work


def get_raw(user_id: int) -> dict[str, str]:
    with session_scope() as s:
        rows = s.scalars(select(Preference).where(Preference.user_id == user_id)).all()
        stored = {r.key: str(r.value) for r in rows if r.key in DEFAULTS}
    return {**DEFAULTS, **stored}


def get_prefs(user_id: int) -> Prefs:
    raw = get_raw(user_id)
    wd, wt = raw["weekly_review"].split(" ")
    work_days, work = [], None
    if raw["work_hours"] != "off":
        days, hours = raw["work_hours"].split(" ")
        work_days, work = _days(days), _range(hours)
    return Prefs(
        briefing_time=_hhmm(raw["briefing_time"]),
        weekly_day=DAYS.index(wd),
        weekly_time=_hhmm(wt),
        quiet=None if raw["quiet_hours"] == "off" else _range(raw["quiet_hours"]),
        default_time=_hhmm(raw["default_time"]),
        work_days=work_days,
        work=work,
        raw=raw,
    )


def set_pref(user_id: int, key: str, value: str) -> str:
    normalised = validate(key, value)
    with session_scope() as s:
        row = s.scalar(select(Preference).where(Preference.user_id == user_id, Preference.key == key))
        if row is None:
            s.add(Preference(user_id=user_id, key=key, value=normalised))
        else:
            row.value = normalised
    return normalised


def reset_pref(user_id: int, key: str) -> None:
    with session_scope() as s:
        row = s.scalar(select(Preference).where(Preference.user_id == user_id, Preference.key == key))
        if row is not None:
            s.delete(row)
