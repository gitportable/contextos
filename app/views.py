"""Shared read-model helpers (kept out of main.py so routers can import them without cycles)."""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Project, SlackConnection, SlackConversation, SlackMessage, User
from app.summaries import open_url

def inbox(db: Session, user: User, include_self: bool = False):
    q = db.query(SlackMessage).filter(SlackMessage.owner_id == user.id, SlackMessage.is_deleted.is_(False))
    return q if include_self else q.filter(SlackMessage.is_self.is_(False))


def message_dicts(db: Session, rows: list[SlackMessage]) -> list[dict]:
    conn_ids = {r.connection_id for r in rows}
    convs = {(c.connection_id, c.channel_id): c for c in db.query(SlackConversation).filter(SlackConversation.connection_id.in_(conn_ids)).all()} if rows else {}
    projs = {p.id: p.name for p in db.query(Project).filter(Project.id.in_({r.project_id for r in rows if r.project_id}))} if rows else {}
    demo = {c.id for c in db.query(SlackConnection).filter(SlackConnection.id.in_(conn_ids), SlackConnection.is_demo.is_(True)).all()} if rows else set()
    out = []
    for r in rows:
        cv = convs.get((r.connection_id, r.channel_id))
        out.append({"id": r.id, "connection_id": r.connection_id, "team_id": r.team_id, "channel_id": r.channel_id,
                    "channel_name": (cv.name if cv and cv.name else r.channel_id), "conv_type": r.conv_type, "slack_ts": r.slack_ts,
                    "thread_ts": r.thread_ts, "reply_count": r.reply_count, "user_id": r.user_id, "author_name": r.author_name,
                    "text": r.text, "priority_score": r.priority_score, "priority_label": r.priority_label, "reason": r.reason,
                    "is_self": r.is_self, "is_bot": r.is_bot, "mentions_me": r.mentions_me, "is_demo": r.connection_id in demo,
                    "project_id": r.project_id, "project_name": projs.get(r.project_id), "open_url": open_url(r, r.connection_id in demo),
                    "handled": r.handled_at is not None,
                    "edited_at": r.edited_at.isoformat() if r.edited_at else None, "source": r.source,
                    "message_created_at": r.message_created_at.isoformat() if r.message_created_at else None,
                    "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None})
    return out


def own_message(db: Session, user: User, message_id: int) -> SlackMessage:
    row = db.query(SlackMessage).filter_by(id=message_id, owner_id=user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Message not found."})
    return row


