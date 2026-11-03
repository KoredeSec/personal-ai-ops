"""Deterministic natural-language date/time parsing.

Why not a library: dateparser was evaluated and rejected (see
ENGINEERING_NOTES.md, Milestone 2). It returned nothing for "next monday" and
"tonight", kept the current clock time for "tomorrow", and swallowed trailing
words when searching sentences. A small strict grammar is more predictable.

Rules (all in local time, Africa/Lagos by default):
  * a date without a time gets the user's default time (09:00)
  * "tonight" 20:00, "this morning" 09:00, "this afternoon" 15:00,
    "this evening" 18:00, "this weekend" Saturday 10:00
  * a weekday ("monday", "next monday", "on mon") is the next occurrence
    strictly after today, or today if "this <weekday>" and the time is still ahead
  * "next week" = Monday of next week; "next month" = 1st of next month
  * a time alone ("8pm") is today if still ahead, else tomorrow
  * a bare hour without am/pm ("at 5") below 8 is read as pm
  * day/month numbers follow the Nigerian/UK order: 20/11 = 20 November
  * a day-month with no year that has passed rolls to next year
  * a resolved time in the past is an error, never silently accepted

Anything outside this grammar returns None so the caller can ask the model
to normalise it or ask the user to rephrase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

_WD_FULL = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_WD_ABBR = r"mon|tues?|wed|thu(?:rs?)?|fri|sat|sun"
_MON = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_ORD = r"(?:st|nd|rd|th)"
_NUMWORD = r"\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_UNIT = r"min(?:ute)?s?|h(?:ou)?rs?|hours?|days?|weeks?|months?"

_DATE = rf"""(?:
    today|tonight|tomorrow|tmrw?|tmr
  | this\s+(?:morning|afternoon|evening|weekend)
  | (?:next|this|coming)\s+(?:week|month|weekend)
  | (?:(?:next|this|coming|on)\s+)?(?:{_WD_FULL})
  | (?:next|this|coming|on)\s+(?:{_WD_ABBR})
  | in\s+(?:{_NUMWORD})\s+(?:{_UNIT})
  | (?:on\s+)?(?:the\s+)?\d{{1,2}}{_ORD}?\s+(?:of\s+)?(?:{_MON})(?:\s+\d{{4}})?
  | (?:on\s+)?(?:{_MON})\s+\d{{1,2}}{_ORD}?(?:,?\s+\d{{4}})?
  | (?:on\s+)?the\s+\d{{1,2}}{_ORD}
  | \d{{4}}-\d{{2}}-\d{{2}}
  | \d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?
)"""
_TIME = r"""(?:
    (?:(?:at|@)\s*)?\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)
  | (?:(?:at|@)\s*)?(?:[01]?\d|2[0-3]):[0-5]\d
  | (?:at|@)\s*\d{1,2}(?![\d:/])
  | (?:at\s+)?(?:noon|midday|midnight)
)"""
_PREP = r"(?:(?P<prep>by|before|due|on|for|from)\s+)?"

_WHEN_RE = re.compile(
    rf"(?<![\w/]){_PREP}(?P<expr>{_DATE}(?:\s*,?\s*{_TIME})?|{_TIME}(?:\s*,?\s*(?:on\s+)?{_DATE})?)(?![\w/])",
    re.IGNORECASE | re.VERBOSE,
)
_TIME_RE = re.compile(rf"(?<![\w/]){_TIME}(?![\w/])", re.IGNORECASE | re.VERBOSE)
RECURRING_RE = re.compile(r"\b(every|daily|weekly|monthly|each)\b", re.IGNORECASE)

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_PART_OF_DAY = {"morning": time(9), "afternoon": time(15), "evening": time(18)}


class WhenError(ValueError):
    """A recognised expression that cannot be used (past, impossible date)."""


@dataclass(frozen=True)
class When:
    at: datetime  # aware, local tz
    phrase: str  # the matched text, for display/debugging
    has_time: bool
    is_deadline: bool  # introduced by "by"/"before"/"due"


@dataclass(frozen=True)
class Extracted:
    when: When | None
    remainder: str  # input with the date phrase removed and tidied


def _parse_time(text: str) -> time | None:
    m = _TIME_RE.search(text)
    if not m:
        return None
    t = m.group(0).lower().replace(".", "").replace("@", "").replace("at", "").strip()
    if t in ("noon", "midday"):
        return time(12)
    if t == "midnight":
        return time(0)
    ampm = None
    if t.endswith(("am", "pm")):
        ampm, t = t[-2:], t[:-2].strip()
    hh, _, mm = t.partition(":")
    hour, minute = int(hh), int(mm or 0)
    if ampm:
        if not 1 <= hour <= 12:
            raise WhenError(f"'{m.group(0)}' isn't a valid time")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif not mm and hour < 8:  # bare "at 5" -> 17:00
        hour += 12
    if hour > 23 or minute > 59:
        raise WhenError(f"'{m.group(0)}' isn't a valid time")
    return time(hour, minute)


def _weekday_index(word: str) -> int:
    return _WEEKDAYS.index(word[:3].lower())


def _month_index(word: str) -> int:
    return _MONTHS.index(word[:3].lower()) + 1


def _safe_date(y: int, m: int, d: int, phrase: str) -> date:
    try:
        return date(y, m, d)
    except ValueError as exc:
        raise WhenError(f"'{phrase}' isn't a real date") from exc


def _resolve_date(dp: str, today: date, now: datetime) -> tuple[date | None, time | None, datetime | None]:
    """Return (date, implied_time, exact_datetime). exact wins when set."""
    dp = re.sub(r"\s+", " ", dp.lower().strip().strip(","))
    dp = re.sub(r"^on ", "", dp)
    if not dp:
        return None, None, None
    if dp == "today":
        return today, None, None
    if dp == "tonight":
        return today, time(20), None
    if dp in ("tomorrow", "tmr", "tmrw", "tmw"):
        return today + timedelta(days=1), None, None
    if m := re.fullmatch(r"this (morning|afternoon|evening)", dp):
        return today, _PART_OF_DAY[m.group(1)], None
    if dp in ("this weekend", "next weekend", "coming weekend"):
        days = (5 - today.weekday()) % 7
        if dp == "next weekend" and days == 0:
            days = 7
        return today + timedelta(days=days), time(10), None
    if dp in ("next week", "coming week"):
        return today + timedelta(days=7 - today.weekday()), None, None
    if dp in ("next month", "coming month"):
        y, mo = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return date(y, mo, 1), None, None
    if m := re.fullmatch(rf"(?:(next|this|coming) )?({_WD_FULL}|{_WD_ABBR})", dp):
        target = _weekday_index(m.group(2))
        days = (target - today.weekday()) % 7
        if days == 0 and m.group(1) != "this":
            days = 7
        return today + timedelta(days=days), None, None
    if m := re.fullmatch(rf"in ({_NUMWORD}) ({_UNIT})", dp):
        n = int(m.group(1)) if m.group(1).isdigit() else _NUMWORDS[m.group(1)]
        unit = m.group(2)
        if unit.startswith("min"):
            return None, None, now + timedelta(minutes=n)
        if unit.startswith("h"):
            return None, None, now + timedelta(hours=n)
        if unit.startswith("d"):
            return today + timedelta(days=n), None, None
        if unit.startswith("w"):
            return today + timedelta(weeks=n), None, None
        months = today.month - 1 + n
        y, mo = today.year + months // 12, months % 12 + 1
        return _safe_date(y, mo, min(today.day, 28), dp), None, None
    if m := re.fullmatch(rf"(?:the )?(\d{{1,2}}){_ORD}? (?:of )?({_MON})(?: (\d{{4}}))?", dp):
        return _roll_year(int(m.group(1)), _month_index(m.group(2)), m.group(3), today, dp), None, None
    if m := re.fullmatch(rf"({_MON}) (\d{{1,2}}){_ORD}?(?:,? (\d{{4}}))?", dp):
        return _roll_year(int(m.group(2)), _month_index(m.group(1)), m.group(3), today, dp), None, None
    if m := re.fullmatch(rf"the (\d{{1,2}}){_ORD}", dp):
        d = int(m.group(1))
        y, mo = today.year, today.month
        if d < today.day:
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        return _safe_date(y, mo, d, dp), None, None
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", dp):
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), dp), None, None
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", dp):
        year = m.group(3)
        if year and len(year) == 2:
            year = "20" + year
        return _roll_year(int(m.group(1)), int(m.group(2)), year, today, dp), None, None
    return None, None, None


def _roll_year(d: int, mo: int, year: str | None, today: date, phrase: str) -> date:
    if year:
        return _safe_date(int(year), mo, d, phrase)
    candidate = _safe_date(today.year, mo, d, phrase)
    return candidate if candidate >= today else _safe_date(today.year + 1, mo, d, phrase)


def resolve(expr: str, now: datetime, default_time: time = time(9), prep: str | None = None) -> When:
    """Resolve a matched expression against `now` (aware, local)."""
    explicit_time = _parse_time(expr)
    date_part = _TIME_RE.sub(" ", expr)
    d, implied, exact = _resolve_date(date_part, now.date(), now)
    tz = now.tzinfo
    if exact is not None:
        at = exact
        has_time = True
    elif d is None:  # time only
        if explicit_time is None:
            raise WhenError(f"I couldn't work out a time from '{expr}'")
        at = datetime.combine(now.date(), explicit_time, tzinfo=tz)
        if at <= now:
            at += timedelta(days=1)
        has_time = True
    else:
        t = explicit_time or implied or default_time
        if explicit_time == time(0) and "midnight" in expr.lower():
            d += timedelta(days=1)
        at = datetime.combine(d, t, tzinfo=tz)
        has_time = explicit_time is not None or implied is not None
        if at <= now and not has_time and d == now.date():
            # "today" with the default time already gone: soonest sensible slot
            at = _next_half_hour(now)
            has_time = True
        evening = implied in (time(18), time(20))  # "this evening" / "tonight"
        if evening and explicit_time is None and at <= now and d == now.date() and now.hour < 23:
            at = _next_half_hour(now)  # "tonight" said at 21:00 still means tonight
        # "this morning" said in the evening stays in the past -> error below
    if at <= now:
        raise WhenError(f"'{expr.strip()}' is in the past ({at:%a %d %b %H:%M})")
    return When(at=at, phrase=expr.strip(), has_time=has_time,
                is_deadline=(prep or "").lower() in ("by", "before", "due"))


def _next_half_hour(now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    add = 30 - base.minute % 30
    return base + timedelta(minutes=add if add >= 10 else add + 30)


def parse_when(text: str, now: datetime, default_time: time = time(9)) -> When | None:
    """Parse text that should be *only* a date/time expression."""
    text = text.strip()
    m = _WHEN_RE.fullmatch(text)
    if not m:
        m2 = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ t](\d{2}:\d{2})", text, re.IGNORECASE)
        if m2:  # model-normalised ISO form
            return resolve(f"{m2.group(1)} {m2.group(2)}", now, default_time)
        return None
    return resolve(m.group("expr"), now, default_time, m.group("prep"))


def extract_when(text: str, now: datetime, default_time: time = time(9)) -> Extracted:
    """Find the longest date/time phrase in a sentence and remove it."""
    matches = list(_WHEN_RE.finditer(text))
    if not matches:
        return Extracted(None, _tidy(text))
    m = max(matches, key=lambda x: (len(x.group(0)), x.start()))
    when = resolve(m.group("expr"), now, default_time, m.group("prep"))
    remainder = _tidy(text[: m.start()] + " " + text[m.end():])
    return Extracted(when, remainder)


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,.;:-")
    text = re.sub(r"\s+(on|at|by|for|from|before|due)$", "", text, flags=re.IGNORECASE)
    return text.strip(" ,.;:-")
