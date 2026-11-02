"""Data lifecycle. Slack is a firehose; ContextOS keeps what is useful and lets the rest expire.

  tier   age                      what happens
  -----  -----------------------  --------------------------------------------------------------------------
  P0-P2  < RETENTION_KEEP_DAYS    full text kept, embedded (Qdrant) and graphed (Neo4j)
  P3     < P3_COMPACT_DAYS        full text kept in PostgreSQL (never embedded)
  P3     >= P3_COMPACT_DAYS       COMPACTED: raw_text dropped, text cut to a short snippet, metadata kept
  P3     >= P3_DELETE_DAYS        deleted
  P0-P2  >= RETENTION_KEEP_DAYS   deleted
  any    per-user cap exceeded    oldest P3, then oldest P2, are deleted until the user is under MAX_MESSAGES_PER_USER
                                  (P0/P1 are never pruned for space; the run logs a warning instead)

Deleting a message also removes its project links and vector/graph entries; reminders and meeting suggestions that came
from it are KEPT (they are the user's to-dos) but lose their link to the source message.
Runs from the worker once a day; every statement is bounded, so one run can never lock the table for long.
"""
import logging
from datetime import timedelta

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app import integrations, pipeline_events, projects
from app.config import settings
from app.models import MessageFeedback, MessageProject, MessageState, OutboundAction, Reminder, SlackMessage, SuggestedEvent, utcnow

log = logging.getLogger("contextos.retention")
CHUNK = 500
MAX_PER_RUN = 20000


def delete_messages(db: Session, ids: list[int]) -> int:
    """Remove messages plus everything that points at them. Does not commit."""
    n = 0
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        db.query(MessageProject).filter(MessageProject.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(MessageState).filter(MessageState.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(MessageFeedback).filter(MessageFeedback.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(Reminder).filter(Reminder.message_id.in_(chunk)).update({"message_id": None}, synchronize_session=False)
        db.query(SuggestedEvent).filter(SuggestedEvent.message_id.in_(chunk)).update({"message_id": None}, synchronize_session=False)
        db.query(OutboundAction).filter(OutboundAction.message_id.in_(chunk)).update({"message_id": None}, synchronize_session=False)
        n += db.query(SlackMessage).filter(SlackMessage.id.in_(chunk)).delete(synchronize_session=False)
    return n


def _ids(db: Session, *conds, limit: int = MAX_PER_RUN, order=None) -> list[int]:
    q = db.query(SlackMessage.id).filter(*conds)
    if order is not None:
        q = q.order_by(*order)
    return [i for (i,) in q.limit(limit).all()]


def run_retention(db: Session) -> dict:
    if not settings.retention_enabled:
        return {"skipped": "retention disabled"}
    now = utcnow()
    report = {"compacted": 0, "deleted_p3": 0, "deleted_old": 0, "deleted_for_cap": 0, "users_over_cap_with_only_p0_p1": 0}

    cutoff_c = now - timedelta(days=settings.retention_p3_compact_days)
    compact_ids = _ids(db, SlackMessage.priority_label == "P3", SlackMessage.compacted_at.is_(None),
                       SlackMessage.message_created_at < cutoff_c, SlackMessage.is_deleted.is_(False))
    for i in range(0, len(compact_ids), CHUNK):
        chunk = compact_ids[i:i + CHUNK]
        report["compacted"] += db.query(SlackMessage).filter(SlackMessage.id.in_(chunk)).update(
            {"raw_text": "", "text": func.substr(SlackMessage.text, 1, settings.snippet_chars), "compacted_at": now}, synchronize_session=False)
    db.commit()

    doomed = _ids(db, SlackMessage.priority_label == "P3", SlackMessage.message_created_at < now - timedelta(days=settings.retention_p3_delete_days))
    report["deleted_p3"] = delete_messages(db, doomed)
    old = _ids(db, SlackMessage.priority_label != "P3", SlackMessage.message_created_at < now - timedelta(days=settings.retention_keep_days))
    report["deleted_old"] = delete_messages(db, old)
    db.commit()
    gone = doomed + old

    # per-user hard cap
    over = (db.query(SlackMessage.owner_id, func.count(SlackMessage.id)).group_by(SlackMessage.owner_id)
            .having(func.count(SlackMessage.id) > settings.max_messages_per_user).all())
    tier = case((SlackMessage.priority_label == "P3", 0), (SlackMessage.priority_label == "P2", 1), else_=9)
    for owner_id, count in over:
        excess = count - settings.max_messages_per_user
        victims = _ids(db, SlackMessage.owner_id == owner_id, tier < 9, limit=min(excess, MAX_PER_RUN),
                       order=(tier.asc(), SlackMessage.message_created_at.asc()))
        report["deleted_for_cap"] += delete_messages(db, victims)
        gone += victims
        if len(victims) < excess:
            report["users_over_cap_with_only_p0_p1"] += 1
            log.warning("retention_cap_unreachable owner=%s over_by=%s (only P0/P1 left; raise MAX_MESSAGES_PER_USER)", owner_id, excess - len(victims))
        pipeline_events.record(db, "retention", "ok", owner_id=owner_id, detail=f"over the {settings.max_messages_per_user}-message cap: pruned {len(victims)} oldest low-priority")
    db.commit()

    for i in range(0, len(gone), 2000):  # external indexes last: PostgreSQL is already consistent
        integrations.delete_message_index(gone[i:i + 2000])
    if gone:
        projects.refresh_stats(db)
    report["pipeline_events_pruned"] = pipeline_events.prune(db)
    db.commit()
    log.info("retention_done %s", report)
    return report
