"""Decision accounting: did the system take a decision off the owner's plate,
or hand one back?

This exists because of early feedback on the project: tasks finished is a
vanity metric, and a busy system can quietly create more work than it
absorbs. The definitions are deliberately simple and written down so they
can be argued with:

REMOVED (the owner didn't have to decide or remember)
  reminder_delivered   a reminder fired at its time
  request_handled      a natural-language request was executed without a follow-up question
  briefing_ranked      the daily briefing ordered the day's priorities (one per briefing)
  followup_surfaced    a due follow-up was raised at the right time

ADDED (the owner now has a decision to make)
  clarification_asked  the system couldn't act without asking
  conflict_flagged     overlapping or overloaded commitments were detected
  overdue_flagged      an overdue item needs rescheduling or dropping
  interpretation_fixed the owner used Undo/Edit (the system got it wrong)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from app.database.models import DecisionEvent, DecisionOutcome
from app.database.session import session_scope

logger = logging.getLogger(__name__)

AGENT = "chief_of_staff"


def record(user_id: int, kind: str, outcome: DecisionOutcome, ref: str | None = None,
           agent: str = AGENT) -> None:
    """Best effort: metrics must never break the action they describe."""
    try:
        with session_scope() as s:
            s.add(DecisionEvent(user_id=user_id, agent=agent, kind=kind, outcome=outcome, ref=ref))
    except Exception:
        logger.exception("Failed to record decision event %s", kind)


def removed(user_id: int, kind: str, ref: str | None = None) -> None:
    record(user_id, kind, DecisionOutcome.REMOVED, ref)


def added(user_id: int, kind: str, ref: str | None = None) -> None:
    record(user_id, kind, DecisionOutcome.ADDED, ref)


@dataclass(frozen=True)
class DecisionSummary:
    removed: int
    added: int
    by_kind: dict[str, int]

    @property
    def net(self) -> int:
        return self.removed - self.added


def summarise(user_id: int, start: datetime, end: datetime) -> DecisionSummary:
    with session_scope() as s:
        rows = s.execute(
            select(DecisionEvent.kind, DecisionEvent.outcome, func.count())
            .where(DecisionEvent.user_id == user_id, DecisionEvent.created_at >= start,
                   DecisionEvent.created_at < end)
            .group_by(DecisionEvent.kind, DecisionEvent.outcome)
        ).all()
    rem = sum(n for _, o, n in rows if o == DecisionOutcome.REMOVED)
    add = sum(n for _, o, n in rows if o == DecisionOutcome.ADDED)
    return DecisionSummary(removed=rem, added=add, by_kind={k: n for k, _, n in rows})
