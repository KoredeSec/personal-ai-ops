"""Rules-first intent recognition.

The common phrasings are matched by explicit patterns: free, instant and
predictable. Only text that matches nothing goes to the model
(see nlu.py), and the model can only choose from the same closed set of
actions, which are then executed by the same deterministic code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, time

from app.agents.chief_of_staff.dates import RECURRING_RE, When, WhenError, extract_when
from app.database.models import Priority

ACTIONS = (
    "add_task", "add_reminder", "add_followup", "remember", "complete_task",
    "list_tasks", "list_overdue", "briefing_today", "plan_tomorrow", "weekly",
    "list_followups", "list_reminders", "list_memory", "research", "help", "unknown",
)


@dataclass(frozen=True)
class Intent:
    action: str
    text: str | None = None  # task title / reminder message / memory content
    when: When | None = None
    when_text: str | None = None  # a time phrase we could not resolve
    when_error: str | None = None
    person: str | None = None
    context: str | None = None
    task_id: int | None = None
    priority: Priority | None = None
    recurring: bool = False
    source: str = "rules"  # "rules" | "model"


_I = re.IGNORECASE

_RESEARCH = re.compile(
    r"^(?:please\s+)?(?:find|search|look\s+up|research|give\s+me|show\s+me|get\s+me|any|what(?:'s|\s+is|\s+are))\b.*"
    r"\b(?:scholarships?|master'?s|msc|erasmus|fellowships?|internships?|programmes?|programs?|opportunit\w*|"
    r"cves?|vulnerabilit\w*|security|aws|azure|cloud|threat\w*|detection|devsecops|dfir|ics|ot\b|llm|"
    r"developments|news|advisor\w*|papers?|grad(?:uate)?\s+(?:roles?|jobs?|positions?)|deadlines?)", _I)
_REMIND = re.compile(r"^(?:please\s+)?(?:remind\s+me|set\s+(?:a\s+)?reminder)(?:\s+(?:to|about|that))?\s+(.+)$", _I)
_FOLLOWUP = re.compile(
    r"^(?:i\s+(?:need|have)\s+to\s+|i\s+should\s+|i\s+must\s+)?"
    r"(?:follow[\s-]?up|check\s+in|get\s+back\s+to|reach\s+out\s+to|touch\s+base)\s+(?:with\s+)?(.+)$", _I)
_TASK = [
    re.compile(r"^(?:add|new|create)\s+(?:a\s+)?(?:task|todo|to-do)\s*:?\s+(.+)$", _I),
    re.compile(r"^(?:todo|to-do|task)\s*:\s*(.+)$", _I),
    re.compile(r"^(?:i\s+need\s+to|i\s+have\s+to|i\s+must|i've\s+got\s+to|i\s+gotta|remember\s+to)\s+(.+)$", _I),
]
_COMPLETE = re.compile(r"^(?:done|completed?|finished|mark|tick(?:\s+off)?)\s+(?:task\s+)?#?(\d+)(?:\s+(?:as\s+)?(?:done|complete))?\.?$", _I)
_REMEMBER = re.compile(r"^(?:remember|note|fyi)(?:\s+that)?\s*[:,]?\s+(.+)$", _I)

_QUERIES: list[tuple[str, re.Pattern]] = [
    ("plan_tomorrow", re.compile(
        r"\b(?:priorities|plan|planning|schedule|agenda)\b.*\btomorrow\b|\btomorrow'?s?\s+(?:plan|priorities|agenda)|"
        r"^what(?:'s|\s+is)\s+(?:on\s+)?(?:for\s+)?tomorrow", _I)),
    ("weekly", re.compile(r"\b(?:weekly\s+review|review\s+(?:my|the|this)\s+week|how\s+did\s+(?:my|this)\s+week\s+go)\b", _I)),
    ("list_overdue", re.compile(r"\boverdue\b|\bwhat(?:'s|\s+have\s+i)\s+(?:missed|slipped)\b", _I)),
    ("briefing_today", re.compile(
        r"^(?:brief\s+me|(?:daily\s+)?briefing|today|my\s+day|plan\s+(?:for\s+)?today|"
        r"what(?:'s|\s+is)\s+(?:on\s+)?(?:for\s+)?today|what\s+should\s+i\s+(?:do|focus\s+on)(?:\s+(?:today|now))?)\b", _I)),
    ("list_followups", re.compile(r"\bfollow[\s-]?ups?\b|\bwho\s+(?:do\s+i\s+need|should\s+i)\s+(?:to\s+)?(?:contact|call|follow)", _I)),
    ("list_reminders", re.compile(r"^(?:show\s+|list\s+)?(?:my\s+)?reminders\??$", _I)),
    ("list_memory", re.compile(r"\bwhat\s+do\s+you\s+(?:know|remember)\s+about\s+me\b|^(?:show\s+)?(?:my\s+)?memory\b", _I)),
    ("list_tasks", re.compile(
        r"\b(?:outstanding|open|pending)\s+(?:commitments|tasks|items|todos)\b|\bmy\s+(?:commitments|tasks|todos|to-dos)\b|"
        r"^(?:show|list)\s+(?:my\s+)?tasks\b|\bon\s+my\s+plate\b|^tasks\??$", _I)),
    ("help", re.compile(r"^(?:help|what\s+can\s+you\s+do\??|commands)$", _I)),
]

_PRIORITY_WORDS = [
    (Priority.URGENT, re.compile(r"\b(?:urgent(?:ly)?|asap|immediately)\b[,!]?", _I)),
    (Priority.HIGH, re.compile(r"\b(?:high[\s-]priority|important|priority)\b[,!]?", _I)),
    (Priority.LOW, re.compile(r"\b(?:low[\s-]priority|whenever|someday|no\s+rush)\b[,!]?", _I)),
]
_PERSON_SPLIT = re.compile(r"\s+(?:about|re:?|regarding|on\s+the|concerning|for)\s+|\s+[-–—:]\s+", _I)


def _priority(text: str) -> tuple[Priority | None, str]:
    for p, pattern in _PRIORITY_WORDS:
        if pattern.search(text):
            return p, re.sub(r"\s+", " ", pattern.sub(" ", text)).strip(" ,")
    return None, text


def _with_when(intent: Intent, text: str, now: datetime, default_time: time) -> Intent:
    """Pull a date phrase out of `text` into intent.when; keep the rest as text."""
    try:
        ex = extract_when(text, now, default_time)
    except WhenError as exc:
        return replace(intent, text=text, when_error=str(exc))
    return replace(intent, text=ex.remainder, when=ex.when)


def _capitalise(s: str | None) -> str | None:
    return s[0].upper() + s[1:] if s else s


def parse(text: str, now: datetime, default_time: time = time(9)) -> Intent:
    """Classify text with rules only. Returns action 'unknown' when unsure."""
    t = text.strip()
    if not t:
        return Intent("unknown")
    recurring = bool(RECURRING_RE.search(t))

    if m := _COMPLETE.match(t):
        return Intent("complete_task", task_id=int(m.group(1)))

    if m := _REMIND.match(t):
        body = m.group(1)
        fu = _FOLLOWUP.match(body)
        intent = _with_when(Intent("add_reminder", recurring=recurring), body, now, default_time)
        if fu:  # "remind me to follow up with X ..." -> reminder + tracked follow-up
            person, _, context = _split_person(_with_when(Intent("x"), fu.group(1), now, default_time).text or "")
            intent = replace(intent, person=person, context=context)
        return replace(intent, text=_capitalise(intent.text))

    if m := _FOLLOWUP.match(t):
        intent = _with_when(Intent("add_followup", recurring=recurring), m.group(1), now, default_time)
        person, _, context = _split_person(intent.text or "")
        return replace(intent, person=person, context=context, text=None)

    for pattern in _TASK:
        if m := pattern.match(t):
            prio, body = _priority(m.group(1))
            intent = _with_when(Intent("add_task", priority=prio, recurring=recurring), body, now, default_time)
            return replace(intent, text=_capitalise(intent.text))

    if m := _REMEMBER.match(t):
        return Intent("remember", text=_capitalise(m.group(1).strip()))

    if _RESEARCH.search(t):
        return Intent("research", text=t)

    for action, pattern in _QUERIES:
        if pattern.search(t):
            return Intent(action)

    return Intent("unknown", text=t)


def _split_person(text: str) -> tuple[str, str, str | None]:
    parts = _PERSON_SPLIT.split(text, maxsplit=1)
    person = parts[0].strip(" ,.")
    context = parts[1].strip(" ,.") if len(parts) > 1 else None
    return person, "", context
