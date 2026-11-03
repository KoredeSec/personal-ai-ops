"""ORM models for the whole platform.

The full schema is created in Milestone 1 so that later milestones add
behaviour, not foundational tables. Every schema change after the initial
migration must go through a new Alembic revision.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UTCDateTime, _utcnow, str_enum


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------
class TaskStatus(enum.StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class Priority(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ReminderStatus(enum.StrEnum):
    PENDING = "pending"
    SENDING = "sending"  # claimed by the dispatcher; prevents double sends
    SENT = "sent"
    CANCELLED = "cancelled"
    FAILED = "failed"


class FollowUpStatus(enum.StrEnum):
    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"


class ProjectStatus(enum.StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class MemoryCategory(enum.StrEnum):
    OBJECTIVE = "objective"
    PROJECT = "project"
    CONSTRAINT = "constraint"
    STUDY = "study"
    COMMITMENT = "commitment"
    COMMUNICATION = "communication"
    PROFILE = "profile"
    OTHER = "other"


class SourceKind(enum.StrEnum):
    RSS = "rss"
    WEBPAGE = "webpage"


class SourceCategory(enum.StrEnum):
    TECHNICAL = "technical"
    OPPORTUNITY = "opportunity"


class TrustLevel(enum.StrEnum):
    OFFICIAL = "official"  # vendor / university / programme / government
    REPUTABLE = "reputable"  # established security research outlets
    COMMUNITY = "community"


class ArticleStatus(enum.StrEnum):
    NEW = "new"
    PROCESSED = "processed"
    SKIPPED = "skipped"  # judged irrelevant or duplicate
    FAILED = "failed"


class OpportunityType(enum.StrEnum):
    MASTERS = "masters"
    SCHOLARSHIP = "scholarship"
    JOB = "job"
    INTERNSHIP = "internship"
    FELLOWSHIP = "fellowship"
    CERTIFICATION = "certification"
    PROGRAMME = "programme"
    OTHER = "other"


class VerificationStatus(enum.StrEnum):
    VERIFIED = "verified"  # confirmed on an official source
    PARTIAL = "partial"
    UNVERIFIED = "unverified"


class ApplicationStatus(enum.StrEnum):
    IDENTIFIED = "identified"
    RESEARCHING = "researching"
    PREPARING = "preparing"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    NOT_ELIGIBLE = "not_eligible"
    EXPIRED = "expired"


class DeliveryStatus(enum.StrEnum):
    SENDING = "sending"  # claimed; outcome unknown until marked
    SENT = "sent"
    RETRY = "retry"  # last attempt failed; may be claimed again
    FAILED = "failed"  # gave up after max attempts


class DecisionOutcome(enum.StrEnum):
    REMOVED = "removed"  # the system decided/handled it; the owner didn't have to
    ADDED = "added"  # the system handed a decision back to the owner


class ExecutionStatus(enum.StrEnum):
    SUCCESS = "success"
    ERROR = "error"


# --------------------------------------------------------------------------
# Identity & preferences
# --------------------------------------------------------------------------
class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    display_name: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Lagos", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    preferences: Mapped[list[Preference]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Preference(TimestampMixin, Base):
    """Structured settings (key -> JSON value), e.g. briefing_time."""

    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[object] = mapped_column(JSON, nullable=False)

    user: Mapped[User] = relationship(back_populates="preferences")


class MemoryItem(TimestampMixin, Base):
    """Explicitly provided facts (objectives, constraints...). User-editable."""

    __tablename__ = "memory_items"
    __table_args__ = (Index("ix_memory_items_user_category", "user_id", "category", "is_active"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    category: Mapped[MemoryCategory] = mapped_column(str_enum(MemoryCategory), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# --------------------------------------------------------------------------
# Chief of Staff
# --------------------------------------------------------------------------
class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(
        str_enum(ProjectStatus), default=ProjectStatus.ACTIVE, nullable=False
    )
    is_long_term_objective: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    target_date: Mapped[datetime | None] = mapped_column(UTCDateTime())

    tasks: Mapped[list[Task]] = relationship(back_populates="project")


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_user_status_due", "user_id", "status", "due_at"),
        Index("ix_tasks_user_scheduled", "user_id", "scheduled_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100))
    priority: Mapped[Priority] = mapped_column(
        str_enum(Priority), default=Priority.MEDIUM, nullable=False
    )
    status: Mapped[TaskStatus] = mapped_column(
        str_enum(TaskStatus), default=TaskStatus.OPEN, nullable=False
    )
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Optional time-block, used for conflict detection between commitments.
    scheduled_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    scheduled_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    project: Mapped[Project | None] = relationship(back_populates="tasks")


class FollowUp(TimestampMixin, Base):
    """A person to contact. The system never contacts them itself."""

    __tablename__ = "followups"
    __table_args__ = (Index("ix_followups_user_status_next", "user_id", "status", "next_follow_up_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    person_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_channel: Mapped[str | None] = mapped_column(String(200))
    context: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[Priority] = mapped_column(
        str_enum(Priority), default=Priority.MEDIUM, nullable=False
    )
    status: Mapped[FollowUpStatus] = mapped_column(
        str_enum(FollowUpStatus), default=FollowUpStatus.PENDING, nullable=False
    )
    last_contact_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_follow_up_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Reminder(TimestampMixin, Base):
    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_status_remind_at", "status", "remind_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    followup_id: Mapped[int | None] = mapped_column(ForeignKey("followups.id", ondelete="SET NULL"))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    remind_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[ReminderStatus] = mapped_column(
        str_enum(ReminderStatus), default=ReminderStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# --------------------------------------------------------------------------
# Research
#
# Data ownership: research sources, articles and opportunities are GLOBAL
# (no user_id). This is a single-owner system; the research corpus is shared
# knowledge, while Chief-of-Staff data (tasks, reminders, follow-ups, memory)
# is per user. If multi-user support is ever needed, add user_id to
# opportunities (application status is personal) via a migration.
# --------------------------------------------------------------------------
class ResearchSource(TimestampMixin, Base):
    __tablename__ = "research_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), unique=True, nullable=False)
    kind: Mapped[SourceKind] = mapped_column(str_enum(SourceKind), nullable=False)
    category: Mapped[SourceCategory] = mapped_column(
        str_enum(SourceCategory), default=SourceCategory.TECHNICAL, nullable=False
    )
    trust_level: Mapped[TrustLevel] = mapped_column(
        str_enum(TrustLevel), default=TrustLevel.REPUTABLE, nullable=False
    )
    topics: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    fetch_interval_minutes: Mapped[int] = mapped_column(Integer, default=360, nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_status: Mapped[str | None] = mapped_column(String(200))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # HTTP caching headers to avoid re-downloading unchanged feeds.
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(200))
    # sha256 of extracted page text, for change detection on watched webpages.
    content_hash: Mapped[str | None] = mapped_column(String(64))

    articles: Mapped[list[ResearchArticle]] = relationship(back_populates="source")


class ResearchArticle(TimestampMixin, Base):
    __tablename__ = "research_articles"
    __table_args__ = (
        Index("ix_research_articles_status_published", "status", "published_at"),
        Index("ix_research_articles_title_hash", "title_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("research_sources.id", ondelete="SET NULL"))
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    # sha256 of the normalised URL - exact duplicate detection.
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sha256 of the normalised title - cross-source duplicate detection.
    title_hash: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    excerpt: Mapped[str | None] = mapped_column(Text)  # untrusted, truncated
    status: Mapped[ArticleStatus] = mapped_column(
        str_enum(ArticleStatus), default=ArticleStatus.NEW, nullable=False
    )
    relevance_score: Mapped[int | None] = mapped_column(Integer)  # 0-10
    topics: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    technical_summary: Mapped[str | None] = mapped_column(Text)
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    affected_technologies: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    practical_implications: Mapped[str | None] = mapped_column(Text)
    suggested_lab: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    model_used: Mapped[str | None] = mapped_column(String(100))
    submitted_manually: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The content contained text that looked like instructions aimed at an AI.
    injection_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Owner feedback: 1 useful, -1 not relevant, None no feedback.
    feedback: Mapped[int | None] = mapped_column(Integer)

    source: Mapped[ResearchSource | None] = relationship(back_populates="articles")


class Opportunity(TimestampMixin, Base):
    __tablename__ = "opportunities"
    __table_args__ = (Index("ix_opportunities_type_status", "opportunity_type", "application_status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    organisation: Mapped[str | None] = mapped_column(String(300))
    opportunity_type: Mapped[OpportunityType] = mapped_column(str_enum(OpportunityType), nullable=False)
    official_url: Mapped[str | None] = mapped_column(String(2000))
    discovered_via_url: Mapped[str | None] = mapped_column(String(2000))
    # Normalised hash of official_url (or name+org) to prevent duplicates.
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    eligibility: Mapped[str | None] = mapped_column(Text)
    funding_info: Mapped[str | None] = mapped_column(Text)
    required_documents: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    relevance: Mapped[str | None] = mapped_column(Text)
    concerns: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        str_enum(VerificationStatus), default=VerificationStatus.UNVERIFIED, nullable=False
    )
    # Which fields are verified vs assumed, e.g. {"funding_info": "verified"}.
    field_verification: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    # field -> verbatim quote from the page that supports it. A field without
    # a quote that appears on the page is never stored.
    evidence: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    verification_notes: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    application_status: Mapped[ApplicationStatus] = mapped_column(
        str_enum(ApplicationStatus), default=ApplicationStatus.IDENTIFIED, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)

    deadlines: Mapped[list[OpportunityDeadline]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan", order_by="OpportunityDeadline.deadline_at"
    )


class OpportunityDeadline(TimestampMixin, Base):
    __tablename__ = "opportunity_deadlines"
    __table_args__ = (Index("ix_opportunity_deadlines_deadline_at", "deadline_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(200), default="Application deadline", nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    notes: Mapped[str | None] = mapped_column(Text)

    opportunity: Mapped[Opportunity] = relationship(back_populates="deadlines")


# --------------------------------------------------------------------------
# Conversation, observability & infrastructure
# --------------------------------------------------------------------------
class ConversationSummary(Base):
    """Rolling compressed context instead of full chat history."""

    __tablename__ = "conversation_summaries"
    __table_args__ = (Index("ix_conversation_summaries_user_agent", "user_id", "agent", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    agent: Mapped[str] = mapped_column(String(50), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class DecisionEvent(Base):
    """One decision taken off the owner's plate, or handed back to them.

    Feeds the weekly review's "decisions removed vs added" metric. Kinds are
    free-form strings (e.g. "reminder_delivered", "clarification_asked") so
    new sources need no migration; see agents/chief_of_staff/decisions.py.
    """

    __tablename__ = "decision_events"
    __table_args__ = (Index("ix_decision_events_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    agent: Mapped[str] = mapped_column(String(50), nullable=False)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    outcome: Mapped[DecisionOutcome] = mapped_column(str_enum(DecisionOutcome), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(200))  # e.g. "task:12"
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class AgentExecutionLog(Base):
    """One row per LLM call or agent action: usage, latency and failures."""

    __tablename__ = "agent_execution_logs"
    __table_args__ = (
        Index("ix_agent_execution_logs_created_at", "created_at"),
        Index("ix_agent_execution_logs_agent_action", "agent", "action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    agent: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(str_enum(ExecutionStatus), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))  # sanitised
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class AuditLog(Base):
    """Security-relevant events: access denials, data changes, approvals."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_event_created", "event_type", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class LLMCacheEntry(Base):
    __tablename__ = "llm_cache"
    __table_args__ = (Index("ix_llm_cache_expires_at", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class NotificationLog(TimestampMixin, Base):
    """Delivery ledger keyed by dedupe_key (e.g. "reminder:42", "briefing:2026-11-04").

    Lifecycle: SENDING (claimed) -> SENT, or back to retry, or FAILED.
    `updated_at` acts as the claim lease: a SENDING row older than the lease
    belongs to a crashed attempt and may be reclaimed. See
    app/services/notifications.py for the exact delivery guarantees.
    """

    __tablename__ = "notification_log"
    __table_args__ = (Index("ix_notification_log_status_updated", "status", "updated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus), default=DeliveryStatus.SENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
