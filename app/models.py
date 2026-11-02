from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; PostgreSQL returns aware ones. Normalise before comparing."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# ---------------------------------------------------------------- identity ---
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(300), default="")   # "" = Google-only account (cannot password-login)
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)  # true once Google vouched for the address
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    projects_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSession(Base):
    """Server-side session. The cookie holds a random token; only its SHA-256 is stored."""
    __tablename__ = "user_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthState(Base):
    """Single-use, short-lived OAuth state. purpose='slack_connect' is bound to the signed-in user who started it;
    purpose='google_login' has no user yet, so it is bound to the browser instead (binder cookie) + nonce + PKCE verifier."""
    __tablename__ = "oauth_states"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(30), default="slack_connect")
    binder_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nonce: Mapped[str | None] = mapped_column(String(100), nullable=True)
    code_verifier: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload: Mapped[str | None] = mapped_column(String(200), nullable=True)   # e.g. the chosen Slack access level
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GoogleCalendarConnection(Base):
    """A user's read-only Google Calendar authorization."""
    __tablename__ = "google_calendar_connections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    google_sub: Mapped[str] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(320))
    access_token_encrypted: Mapped[str] = mapped_column(Text, default="")
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, default="")
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="connected", index=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ------------------------------------------------------------------- slack ---
class SlackConnection(Base):
    """One person's authorization of ContextOS in one Slack workspace."""
    __tablename__ = "slack_connections"
    __table_args__ = (UniqueConstraint("owner_id", "team_id", name="uq_connection_owner_team"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    team_id: Mapped[str] = mapped_column(String(100), index=True)
    team_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    slack_user_id: Mapped[str] = mapped_column(String(100), index=True)
    bot_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_token_encrypted: Mapped[str] = mapped_column(Text, default="")
    user_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bot_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    granted_user_scopes: Mapped[str] = mapped_column(Text, default="")
    granted_bot_scopes: Mapped[str] = mapped_column(Text, default="")
    access_level: Mapped[str] = mapped_column(String(20), default="recommended")  # essential | recommended | with_replies
    # connected | needs_reauth | revoked | disconnected
    status: Mapped[str] = mapped_column(String(30), default="connected", index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    provider: Mapped[str] = mapped_column(String(20), default="slack", index=True)  # slack | gmail (gmail: team_id="gmail:<addr>", slack_user_id=<addr>)
    # sync progress shown in the UI: idle | queued | running | rate_limited | error
    sync_state: Mapped[str] = mapped_column(String(20), default="idle")
    sync_total_conversations: Mapped[int] = mapped_column(Integer, default=0)
    sync_done_conversations: Mapped[int] = mapped_column(Integer, default=0)
    messages_synced: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def scopes(self, kind: str = "user") -> set[str]:
        raw = self.granted_user_scopes if kind == "user" else self.granted_bot_scopes
        return {s for s in (raw or "").split(",") if s}

    def has_scope(self, scope: str, kind: str = "user") -> bool:
        return scope in self.scopes(kind)


class SlackConversation(Base):
    """A conversation the connected user belongs to, plus its sync checkpoints."""
    __tablename__ = "slack_conversations"
    __table_args__ = (UniqueConstraint("connection_id", "channel_id", name="uq_conversation_connection_channel"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("slack_connections.id"), index=True)
    channel_id: Mapped[str] = mapped_column(String(100), index=True)
    conv_type: Mapped[str] = mapped_column(String(20), default="public_channel")  # public_channel|private_channel|im|mpim
    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    peer_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # false once the user is no longer a member
    # --- checkpoints (Slack ts strings; compared as Decimal) ---
    backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    backfill_cursor_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)  # resume boundary (newest->oldest paging)
    backfill_top_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)     # newest ts seen during backfill
    high_watermark_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)   # everything <= this is imported
    messages_synced: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SlackUser(Base):
    """Workspace-level name cache so mentions and authors render as names, not IDs."""
    __tablename__ = "slack_users"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_slack_user_team_user"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    display_name: Mapped[str] = mapped_column(String(300), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventReceipt(Base):
    __tablename__ = "event_receipts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    team_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SlackMessage(Base):
    """A message as seen by ONE ContextOS user (owner). Priority is personal (mentions, DMs), so rows are per connection."""
    __tablename__ = "slack_messages"
    __table_args__ = (
        UniqueConstraint("connection_id", "channel_id", "slack_ts", name="uq_slack_message_identity"),
        Index("ix_msg_owner_inbox", "owner_id", "is_deleted", "priority_score"),
        Index("ix_msg_thread", "connection_id", "channel_id", "thread_ts"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("slack_connections.id"), index=True)
    team_id: Mapped[str] = mapped_column(String(100), index=True)
    channel_id: Mapped[str] = mapped_column(String(100), index=True)
    conv_type: Mapped[str] = mapped_column(String(20), default="public_channel")
    slack_ts: Mapped[str] = mapped_column(String(40))
    thread_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)  # Slack author
    author_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")        # normalised (mentions/links resolved)
    raw_text: Mapped[str] = mapped_column(Text, default="")    # exactly what Slack sent
    priority_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    priority_label: Mapped[str] = mapped_column(String(2), default="P3", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)       # written by the connected user
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    mentions_me: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source: Mapped[str] = mapped_column(String(20), default="event")   # event | history | demo
    source_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    message_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # Qdrant
    compacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # retention: raw text dropped


class MessageState(Base):
    """The user's own triage of one message: marked done, or snoozed until later. Kept in its own table so the (large)
    messages table needs no migration and a re-sync never wipes what the user already dealt with."""
    __tablename__ = "message_states"
    __table_args__ = (UniqueConstraint("owner_id", "message_id", name="uq_message_state"),
                      Index("ix_message_state_owner", "owner_id", "handled_at", "snoozed_until"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("slack_messages.id", ondelete="CASCADE"), index=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MessageFeedback(Base):
    """The user's explicit label on why a message was (not) worth their attention. The ranker learns from these from day one."""
    __tablename__ = "message_feedback"
    __table_args__ = (UniqueConstraint("owner_id", "message_id", name="uq_message_feedback"),
                      Index("ix_message_feedback_owner_sender", "owner_id", "sender_key"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("slack_messages.id", ondelete="CASCADE"), index=True)
    sender_key: Mapped[str] = mapped_column(String(400))
    label: Mapped[str] = mapped_column(String(20))   # not_urgent | wrong_person | not_mine | always_show
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("slack_messages.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|completed|cancelled
    auto_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)  # one auto reminder per message
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SuggestedEvent(Base):
    __tablename__ = "suggested_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("slack_messages.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    proposed_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=False)  # time was guessed, not stated in the message
    status: Mapped[str] = mapped_column(String(20), default="suggested", index=True)
    auto_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    source_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssistantNote(Base):
    __tablename__ = "assistant_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    source_message_ids: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessingJob(Base):
    """Durable PostgreSQL-backed queue. Workers claim jobs with FOR UPDATE SKIP LOCKED."""
    __tablename__ = "processing_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(80), index=True)
    connection_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)     # set for per-user jobs (projects, summaries)
    team_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)  # set for workspace-level slack_event jobs
    payload_json: Mapped[str] = mapped_column(Text)
    # Unique while a job is queued/processing; cleared when it finishes. Prevents duplicate sync/message jobs.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
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
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key", name="uq_action_idempotency"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    connection_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action_type: Mapped[str] = mapped_column(String(50), index=True)
    team_id: Mapped[str] = mapped_column(String(100), index=True)
    channel_id: Mapped[str] = mapped_column(String(100), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("slack_messages.id"), nullable=True)
    send_as: Mapped[str] = mapped_column(String(10), default="user")
    request_json: Mapped[str] = mapped_column(Text)
    # sending | sent | failed | unknown (timeout: may have been delivered) | confirmed | simulated (demo)
    status: Mapped[str] = mapped_column(String(30), default="requested", index=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- projects ---
class Project(Base):
    """A topic that keeps coming up in the user's messages. status: auto | confirmed | stale | archived."""
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("owner_id", "slug", name="uq_project_owner_slug"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="auto", index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    channel_count: Mapped[int] = mapped_column(Integer, default=0)
    author_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary_dirty: Mapped[bool] = mapped_column(Boolean, default=True)
    last_summary_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectKeyword(Base):
    __tablename__ = "project_keywords"
    __table_args__ = (UniqueConstraint("project_id", "keyword", name="uq_project_keyword"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    keyword: Mapped[str] = mapped_column(String(60))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


class MessageProject(Base):
    """Message <-> project link (a message can belong to several projects)."""
    __tablename__ = "message_projects"
    __table_args__ = (UniqueConstraint("message_id", "project_id", name="uq_message_project"),
                      Index("ix_mp_project", "project_id", "message_id"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("slack_messages.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    hits: Mapped[int] = mapped_column(Integer, default=1)


class ProjectSummary(Base):
    """Rolling, versioned summary. Every claim carries the message ids it came from."""
    __tablename__ = "project_summaries"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_summary_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    key_points_json: Mapped[str] = mapped_column(Text, default="[]")
    open_items_json: Mapped[str] = mapped_column(Text, default="[]")
    status_label: Mapped[str] = mapped_column(String(20), default="unknown")   # on_track | at_risk | blocked | unknown
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    mode: Mapped[str] = mapped_column(String(20), default="extractive")        # llm | extractive
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    covers_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    covers_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LlmUsage(Base):
    """Per-user, per-day LLM call counter (the per-user cap that protects a shared free-tier quota)."""
    __tablename__ = "llm_usage"
    __table_args__ = (UniqueConstraint("owner_id", "day", name="uq_llm_usage_owner_day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day: Mapped[str] = mapped_column(String(10))
    calls: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- pipeline ---
class PipelineEvent(Base):
    """One row per stage a message / sync / sign-in passes through. Drives the live pipeline page."""
    __tablename__ = "pipeline_events"
    __table_args__ = (Index("ix_pe_owner_id", "owner_id", "id"), Index("ix_pe_team_id", "team_id", "id"))
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(12), default="ok")     # ok | skipped | error
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class WorkerHeartbeat(Base):
    """Lets the UI say whether a worker is running, and lets the demo fall back to in-process processing if not."""
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    jobs_done: Mapped[int] = mapped_column(Integer, default=0)
