"""One definition of "needs attention", shared by the inbox, the dashboard counters and the assistant.

The product promise is to LESSEN the burden of keeping up, so these rules are deliberately strict:

  * urgent  = a critical incident (P0), or a high-priority (P1) message addressed to the user, from the last
              URGENT_WINDOW_HOURS, not written by a bot, not already done and not snoozed;
  * one card per conversation (a 12-message thread is one thing to look at, not twelve notifications);
  * anything the user marked Done or snoozed disappears from every triage view until the snooze ends.
"""
from datetime import timedelta

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app import ranker
from app.models import as_utc, MessageFeedback, MessageState, SlackConnection, SlackMessage, utcnow


def inbox_query(db: Session, owner_id: int, include_self: bool = False):
    q = db.query(SlackMessage).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False))
    return q if include_self else q.filter(SlackMessage.is_self.is_(False))


def untriaged(db: Session, owner_id: int, query):
    """Drop what the user already dealt with: marked done, or snoozed and not due back yet."""
    hidden = (db.query(MessageState.message_id)
              .filter(MessageState.owner_id == owner_id, or_(MessageState.handled_at.isnot(None), MessageState.snoozed_until > utcnow()))
              .scalar_subquery())
    return query.filter(~SlackMessage.id.in_(hidden))


def when_clause():
    """When the message happened; falls back to when we stored it if the source gave no timestamp."""
    return func.coalesce(SlackMessage.message_created_at, SlackMessage.ingested_at)


def directed_clause():
    """Aimed at the user: an @mention, a DM / group DM, or an email written to them personally."""
    return or_(SlackMessage.mentions_me.is_(True), SlackMessage.conv_type.in_(("im", "mpim")))


def urgent_clause():
    """What really needs a person: critical incidents, or high-priority items addressed to them, and recent.
    Channel chatter that merely sounds pushy ("please review ASAP" in a 200-person channel), bots and week-old messages
    are not urgent, however they scored."""
    since = utcnow() - timedelta(hours=settings.urgent_window_hours)
    return and_(SlackMessage.is_bot.is_(False), SlackMessage.is_self.is_(False), when_clause() >= since,
                or_(SlackMessage.priority_label == "P0", and_(SlackMessage.priority_label == "P1", directed_clause())))


def collapse_conversations(rows: list[SlackMessage]) -> list[tuple[SlackMessage, int]]:
    """One card per conversation (thread, or channel when there is no thread), keeping its highest-ranked message and
    counting the rest. `rows` must arrive best-first."""
    groups: dict[tuple, list[SlackMessage]] = {}
    for r in rows:
        groups.setdefault((r.connection_id, r.channel_id, r.thread_ts or ""), []).append(r)
    return [(g[0], len(g) - 1) for g in groups.values()]


def pinned_ids(db: Session, owner_id: int) -> set[int]:
    """Messages the user labelled "Always show": they stay in Urgent until they mark them Done."""
    return {i for (i,) in db.query(MessageFeedback.message_id).filter(MessageFeedback.owner_id == owner_id, MessageFeedback.label == "always_show").all()}


def urgent_view(db: Session, owner_id: int, *, expand: bool = False, source: str | None = None) -> tuple[list[tuple[SlackMessage, int]], int]:
    """(cards to show, total urgent conversations). The total is what every "N urgent" counter displays.

    The rules above GATE who can be urgent; the ranker then orders them by their score *right now* and drops anything that
    has faded (an old request with no deadline) or that the user's labels pushed down. P0 incidents and pinned items stay."""
    pins = pinned_ids(db, owner_id)
    cond = urgent_clause()
    if pins:
        cond = or_(cond, SlackMessage.id.in_(pins))
    q = untriaged(db, owner_id, inbox_query(db, owner_id)).filter(cond)
    if source:
        q = q.join(SlackConnection, SlackConnection.id == SlackMessage.connection_id).filter(SlackConnection.provider == source)
    rows = q.order_by(desc(SlackMessage.priority_score), desc(SlackMessage.message_created_at)).limit(400).all()
    now, shifts = utcnow(), ranker.sender_shifts(db, owner_id)
    ranked = []
    for r in rows:
        live = ranker.live_score(r, now, shifts)
        if r.id in pins or r.priority_label == "P0" or live >= ranker.LIVE_URGENT_AT:
            ranked.append((live if r.id not in pins else 101, r))
    ranked.sort(key=lambda x: (x[0], as_utc(x[1].message_created_at or x[1].ingested_at)), reverse=True)
    pairs = collapse_conversations([r for _, r in ranked])
    return (pairs if expand else pairs[:settings.urgent_max_items]), len(pairs)


def quiet_count(db: Session, owner_id: int, hours: int = 48) -> int:
    """Recent non-urgent, non-bot messages still untriaged: the pile the assistant tells the user can wait."""
    q = untriaged(db, owner_id, inbox_query(db, owner_id)).filter(
        SlackMessage.is_bot.is_(False), when_clause() >= utcnow() - timedelta(hours=hours),
        ~urgent_clause())
    return q.count()
