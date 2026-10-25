"""Database models. Schema works on both SQLite and PostgreSQL.

All datetimes are stored in UTC and come back timezone-aware.
Everything that belongs to a candidate is deleted with it (ON DELETE CASCADE +
ORM cascades), and the resume file is removed in services/candidates.py.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores UTC, always returns aware datetimes (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class StrEnum(str, enum.Enum):
    def __str__(self) -> str:
        return self.value


class ParseStatus(StrEnum):
    none = "none"
    ok = "ok"
    partial = "partial"
    failed = "failed"


class Channel(StrEnum):
    email = "email"
    sms = "sms"


class Direction(StrEnum):
    inbound = "inbound"
    outbound = "outbound"


class MessageStatus(StrEnum):
    pending_review = "pending_review"  # AI draft waiting for recruiter
    queued = "queued"  # approved, waiting for quiet hours to end
    sent = "sent"
    failed = "failed"
    received = "received"  # inbound
    discarded = "discarded"


class Approval(StrEnum):
    auto = "auto"  # AI-generated, auto-sent
    human_approved = "human_approved"  # AI-generated, recruiter approved (maybe edited)
    human_written = "human_written"  # recruiter wrote it
    none = "none"  # inbound / not yet decided


class EngagementStatus(StrEnum):
    active = "active"
    interview_scheduled = "interview_scheduled"
    opted_out = "opted_out"
    closed = "closed"


class InterviewStatus(StrEnum):
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"
    incomplete = "incomplete"
    no_show = "no_show"
    cancelled = "cancelled"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    required_skills: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), index=True)
    email: Mapped[str] = mapped_column(String(320), default="", index=True)
    phone: Mapped[str] = mapped_column(String(40), default="", index=True)
    summary_notes: Mapped[str] = mapped_column(Text, default="")
    linkedin_url: Mapped[str] = mapped_column(String(500), default="")
    github_url: Mapped[str] = mapped_column(String(500), default="")
    portfolio_url: Mapped[str] = mapped_column(String(500), default="")
    other_links: Mapped[list] = mapped_column(JSON, default=list)
    preferred_channel: Mapped[str] = mapped_column(String(10), default=Channel.email.value)
    timezone: Mapped[str] = mapped_column(String(64), default="")  # empty -> APP_TIMEZONE

    resume_path: Mapped[str] = mapped_column(String(500), default="")  # relative to UPLOAD_DIR
    resume_filename: Mapped[str] = mapped_column(String(300), default="")
    resume_text: Mapped[str] = mapped_column(Text, default="")
    resume_parse_status: Mapped[str] = mapped_column(String(10), default=ParseStatus.none.value)
    resume_parse_warnings: Mapped[str] = mapped_column(Text, default="")

    send_mode_override: Mapped[str | None] = mapped_column(String(10), nullable=True)  # auto|review|None
    agent_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False)
    attention_reason: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    engagements: Mapped[list["Engagement"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan", passive_deletes=True
    )
    messages: Mapped[list["Message"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.created_at",
    )
    interviews: Mapped[list["Interview"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Interview.scheduled_at.desc()",
    )
    activities: Mapped[list["ActivityLog"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ActivityLog.created_at.desc()",
    )


class Engagement(Base):
    """A candidate being contacted about a specific job."""

    __tablename__ = "engagements"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(10), default=Channel.email.value)
    status: Mapped[str] = mapped_column(String(30), default=EngagementStatus.active.value)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="engagements")
    job: Mapped[Job] = relationship()
    messages: Mapped[list["Message"]] = relationship(back_populates="engagement", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    engagement_id: Mapped[int | None] = mapped_column(
        ForeignKey("engagements.id", ondelete="CASCADE"), nullable=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(10))
    channel: Mapped[str] = mapped_column(String(10))
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    approval: Mapped[str] = mapped_column(String(20), default=Approval.none.value)
    hold_reason: Mapped[str] = mapped_column(Text, default="")  # why it is in review / queued
    error: Mapped[str] = mapped_column(Text, default="")
    provider_message_id: Mapped[str] = mapped_column(String(500), default="", index=True)
    in_reply_to: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    candidate: Mapped[Candidate] = relationship(back_populates="messages")
    engagement: Mapped[Engagement | None] = relationship(back_populates="messages")


class Interview(Base):
    __tablename__ = "interviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    engagement_id: Mapped[int | None] = mapped_column(
        ForeignKey("engagements.id", ondelete="SET NULL"), nullable=True
    )
    token: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default=InterviewStatus.scheduled.value, index=True)
    briefing: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str] = mapped_column(String(10), default=Channel.email.value)  # where the link goes
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    question_count: Mapped[int] = mapped_column(Integer, default=0)
    end_reason: Mapped[str] = mapped_column(String(200), default="")
    reminder_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="interviews")
    job: Mapped[Job] = relationship()
    transcript: Mapped[list["TranscriptEntry"]] = relationship(
        back_populates="interview",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TranscriptEntry.seq",
    )
    evaluation: Mapped["InterviewEvaluation | None"] = relationship(
        back_populates="interview", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )


class TranscriptEntry(Base):
    __tablename__ = "transcript_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[int] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))  # agent | candidate | system
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    interview: Mapped[Interview] = relationship(back_populates="transcript")


class InterviewEvaluation(Base):
    __tablename__ = "interview_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[int] = mapped_column(
        ForeignKey("interviews.id", ondelete="CASCADE"), unique=True, index=True
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    strengths: Mapped[list] = mapped_column(JSON, default=list)
    concerns: Mapped[list] = mapped_column(JSON, default=list)
    suggested_score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1-5, recommendation only
    reasoning: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    interview: Mapped[Interview] = relationship(back_populates="evaluation")


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(20))  # ai | recruiter | system | candidate
    summary: Mapped[str] = mapped_column(String(500))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    candidate: Mapped[Candidate] = relationship(back_populates="activities")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
