from datetime import datetime, timezone
from sqlalchemy import DateTime, Integer, String, Text, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

def utcnow():
    return datetime.now(timezone.utc)

class EventReceipt(Base):
    __tablename__ = "event_receipts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    team_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class SlackMessage(Base):
    __tablename__ = "slack_messages"
    __table_args__ = (UniqueConstraint("team_id", "channel_id", "slack_ts", name="uq_slack_message_identity"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[str] = mapped_column(String(100), index=True)
    channel_id: Mapped[str] = mapped_column(String(100), index=True)
    slack_ts: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    priority_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    priority_label: Mapped[str] = mapped_column(String(2), default="P3", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    source_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    message_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("slack_messages.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class SuggestedEvent(Base):
    __tablename__ = "suggested_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("slack_messages.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    proposed_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(20), default="suggested", index=True)
    source_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class AssistantNote(Base):
    __tablename__ = "assistant_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    source_message_ids: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class ProcessingJob(Base):
    """Durable PostgreSQL-backed queue. Workers claim jobs with row locks."""
    __tablename__ = "processing_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(80), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class OutboundAction(Base):
    """Audit trail for approved external actions such as Slack replies."""
    __tablename__ = "outbound_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_type: Mapped[str] = mapped_column(String(50), index=True)
    team_id: Mapped[str] = mapped_column(String(100), index=True)
    channel_id: Mapped[str] = mapped_column(String(100), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("slack_messages.id"), nullable=True)
    request_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="requested", index=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
