"""Shared response shapes."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import ranker

from app.models import SlackConnection, SlackConversation, SlackMessage


def _why(r: SlackMessage) -> str:
    """One plain sentence, computed from the message as it is now (so older rows read the same as new ones)."""
    now = datetime.now(timezone.utc)
    f = ranker.extract(r.text, r.message_created_at or r.ingested_at, now, is_dm=(r.conv_type == "im"), mentions_me=bool(r.mentions_me), is_bot=bool(r.is_bot))
    return ranker.explain(f, r.author_name)


def message_dicts(db: Session, rows: list[SlackMessage]) -> list[dict]:
    conn_ids = {r.connection_id for r in rows}
    convs = {(c.connection_id, c.channel_id): c for c in db.query(SlackConversation).filter(SlackConversation.connection_id.in_(conn_ids)).all()} if rows else {}
    conns = {c.id: c for c in db.query(SlackConnection).filter(SlackConnection.id.in_(conn_ids)).all()} if rows else {}
    demo = {i for i, c in conns.items() if c.is_demo}
    out = []
    for r in rows:
        cv = convs.get((r.connection_id, r.channel_id))
        out.append({"id": r.id, "connection_id": r.connection_id, "team_id": r.team_id, "channel_id": r.channel_id,
                    "channel_name": (cv.name if cv and cv.name else r.channel_id), "conv_type": r.conv_type, "slack_ts": r.slack_ts,
                    "thread_ts": r.thread_ts, "reply_count": r.reply_count, "user_id": r.user_id, "author_name": r.author_name,
                    "text": r.text, "priority_score": r.priority_score, "priority_label": r.priority_label, "reason": _why(r),
                    "is_self": r.is_self, "is_bot": r.is_bot, "mentions_me": r.mentions_me, "is_demo": r.connection_id in demo, "provider": conns[r.connection_id].provider if r.connection_id in conns else "slack",
                    "edited_at": r.edited_at.isoformat() if r.edited_at else None, "source": r.source,
                    "message_created_at": r.message_created_at.isoformat() if r.message_created_at else None,
                    "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None})
    return out
