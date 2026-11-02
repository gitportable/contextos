"""Stage log behind the live pipeline page. record() only ADDS a row to the caller's session: it commits together
with the work it describes (so the page never shows a stage that was rolled back) and it never raises."""
import logging
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PipelineEvent, utcnow

log = logging.getLogger("contextos.events_log")

# Ordered layers the page draws, left to right. (stage, human label, group)
LAYERS = [
    ("login", "Sign-in", "Identity"),
    ("slack_connected", "Source connected (Slack/Gmail)", "Identity"),
    ("received", "Event received + verified", "Ingress"),
    ("queued", "Durable queue (PostgreSQL)", "Ingress"),
    ("fanned_out", "Fan-out to members", "Ingress"),
    ("sync_page", "History sync page", "Ingress"),
    ("ingested", "Normalised + de-duplicated", "Processing"),
    ("scored", "Priority scored", "Processing"),
    ("extracted", "Reminders / meetings", "Processing"),
    ("indexed_vector", "Vector index (Qdrant)", "Storage"),
    ("indexed_graph", "Graph (Neo4j)", "Storage"),
    ("retention", "Retention / cleanup", "Storage"),
    ("project_linked", "Linked to projects", "Intelligence"),
    ("semantic_linked", "Semantic project links (Qdrant)", "Intelligence"),
    ("graph_boost", "Collaborator boost (Neo4j)", "Intelligence"),
    ("semantic_boost", "Urgency echo (Qdrant)", "Intelligence"),
    ("summarized", "Project summary (LLM)", "Intelligence"),
    ("answered", "Assistant answer", "Intelligence"),
]
STAGE_NAMES = [s for s, _, _ in LAYERS]
TEAM_LEVEL_STAGES = ("received", "queued")


def record(db: Session, stage: str, status: str = "ok", *, owner_id: int | None = None, team_id: str | None = None,
           trace_id: str | None = None, message_id: int | None = None, detail: str = "") -> None:
    try:
        db.add(PipelineEvent(owner_id=owner_id, team_id=team_id, trace_id=(trace_id or None) and str(trace_id)[:200], stage=stage,
                             status=status, message_id=message_id, detail=(detail or "")[:500]))
    except Exception:  # logging must never break the pipeline
        log.exception("pipeline_event_failed stage=%s", stage)


def prune(db: Session) -> int:
    """Age + per-user cap, so the log cannot grow without bound."""
    cutoff = utcnow() - timedelta(days=settings.pipeline_event_retention_days)
    n = db.query(PipelineEvent).filter(PipelineEvent.created_at < cutoff).delete(synchronize_session=False)
    heavy = (db.query(PipelineEvent.owner_id, func.count(PipelineEvent.id)).filter(PipelineEvent.owner_id.isnot(None))
             .group_by(PipelineEvent.owner_id).having(func.count(PipelineEvent.id) > settings.pipeline_event_cap_per_user).all())
    for owner_id, _ in heavy:
        keep_from = (db.query(PipelineEvent.id).filter(PipelineEvent.owner_id == owner_id).order_by(PipelineEvent.id.desc())
                     .offset(settings.pipeline_event_cap_per_user).limit(1).scalar())
        if keep_from:
            n += db.query(PipelineEvent).filter(PipelineEvent.owner_id == owner_id, PipelineEvent.id <= keep_from).delete(synchronize_session=False)
    # team-level rows have no owner: age-based only (cap them too)
    team_total = db.query(func.count(PipelineEvent.id)).filter(PipelineEvent.owner_id.is_(None)).scalar() or 0
    if team_total > settings.pipeline_event_cap_per_user * 2:
        keep_from = (db.query(PipelineEvent.id).filter(PipelineEvent.owner_id.is_(None)).order_by(PipelineEvent.id.desc())
                     .offset(settings.pipeline_event_cap_per_user).limit(1).scalar())
        if keep_from:
            n += db.query(PipelineEvent).filter(PipelineEvent.owner_id.is_(None), PipelineEvent.id <= keep_from).delete(synchronize_session=False)
    return n
