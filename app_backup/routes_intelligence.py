"""User-facing intelligence endpoints. Open to every signed-in user, always owner-scoped.
Each response says which engine answered (`source`), and every one still works from PostgreSQL alone when a service is off."""
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import auth, integrations, intelligence, item_events, ratelimit
from app.config import settings
from app.database import get_db
from app.jobs import INDEX_BACKFILL, enqueue_job
from app.models import SlackMessage, User

router = APIRouter(tags=["intelligence"])


class RankingFeedbackIn(BaseModel):
    label: Literal["not_urgent", "wrong_person", "not_mine", "always_show"]


def _err(status: int, code: str, message: str):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _limit(user: User, scope: str, per: int, window: int, msg: str = "") -> None:
    try:
        ratelimit.hit(scope, str(user.id), per, window, msg)
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc


@router.get("/api/search")
def search(q: str = "", limit: int = 30, priority: str | None = None, source: str | None = None,
           user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Search by words AND by meaning. 'fix the login problem' also finds 'users cannot sign in after the deploy'."""
    if source not in (None, "", "slack", "gmail"):
        raise _err(400, "bad_source", "source must be slack or gmail")
    _limit(user, "search:user", 90, 60, "Searching too fast. Wait a moment.")
    return intelligence.smart_search(db, user, q, limit=limit, priority=priority, source=source or None)


@router.get("/messages/{message_id}/related")
def related(message_id: int, limit: int = 6, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    m = db.query(SlackMessage).filter_by(id=message_id, owner_id=user.id).first()
    if not m or m.is_deleted:
        raise _err(404, "not_found", "Message not found.")
    _limit(user, "search:user", 90, 60, "Searching too fast. Wait a moment.")
    return intelligence.related_messages(db, user, m, limit=max(1, min(limit, 12)))


@router.post("/messages/{message_id}/feedback")
def ranking_feedback(message_id: int, data: RankingFeedbackIn, user: User = Depends(auth.current_user),
                     db: Session = Depends(get_db)):
    message = db.query(SlackMessage).filter_by(id=message_id, owner_id=user.id, is_deleted=False).first()
    if not message:
        raise _err(404, "not_found", "Message not found.")
    item_events.record_message_event(db, owner_id=user.id, message_id=message.id, event_type="feedback",
                                    payload={"label": data.label})
    db.commit()
    return {"ok": True, "message_id": message.id, "label": data.label}


@router.get("/api/projects/merge-suggestions")
def merge_suggestions(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return intelligence.merge_suggestions(db, user)


@router.get("/api/people")
def people(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return intelligence.people_overview(db, user)


@router.get("/api/people/detail")
def person(name: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if not name.strip():
        raise _err(400, "bad_name", "Give a name.")
    return intelligence.person_detail(db, user, name)


@router.get("/api/intelligence/status")
def status(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """What the smart features can do right now, for this user's data. Never reveals server addresses or credentials."""
    uid = user.id
    live = db.query(func.count(SlackMessage.id)).filter(SlackMessage.owner_id == uid, SlackMessage.is_deleted.is_(False), SlackMessage.priority_label != "P3").scalar() or 0
    q = integrations.qdrant_stats(uid)
    n = integrations.neo4j_stats(uid)

    def state(stats, on):
        if not on:
            return "off"
        return "error" if stats is None or "error" in stats else "on"
    return {
        "eligible_messages": live,
        "qdrant": {"state": state(q, settings.qdrant_enabled), **({} if not q or "error" in q else q)},
        "neo4j": {"state": state(n, settings.neo4j_enabled), **({} if not n or "error" in n else n)},
        "features": [
            {"id": "search", "label": "Search by meaning", "engine": "Qdrant", "on": settings.qdrant_enabled,
             "does": "Finds messages that mean the same thing even when the words differ."},
            {"id": "related", "label": "Related messages (Slack ↔ Gmail)", "engine": "Qdrant", "on": settings.qdrant_enabled,
             "does": "“More like this” across both apps."},
            {"id": "merge", "label": "Duplicate project suggestions", "engine": "Qdrant", "on": settings.qdrant_enabled,
             "does": "Spots two projects whose summaries describe the same work."},
            {"id": "echo", "label": "Urgency echo", "engine": "Qdrant", "on": settings.qdrant_enabled,
             "does": "A quiet message that repeats an urgent one is raised a little, with the reason shown."},
            {"id": "people", "label": "People & who to ask", "engine": "Neo4j", "on": settings.neo4j_enabled,
             "does": "Who is active across which projects, and who works with whom."},
            {"id": "risk", "label": "Single-point-of-failure warnings", "engine": "Neo4j", "on": settings.neo4j_enabled,
             "does": "Warns when one person wrote most of a project's messages."},
            {"id": "collab", "label": "Key-collaborator priority boost", "engine": "Neo4j", "on": settings.neo4j_enabled,
             "does": "People who connect several projects get their messages noticed."},
        ],
    }


@router.post("/api/intelligence/rebuild")
def rebuild(background: BackgroundTasks, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Catch Qdrant and Neo4j up with everything already stored (for example after switching them on)."""
    _limit(user, "expensive:user", settings.rl_expensive_user_per_hour, 3600)
    if not (settings.qdrant_enabled or settings.neo4j_enabled):
        raise _err(503, "not_configured", "Neither Qdrant nor Neo4j is switched on for this server. Ask the administrator.")
    job = enqueue_job(db, INDEX_BACKFILL, {"owner_id": user.id, "force_graph": True}, owner_id=user.id, dedupe_key=f"index_backfill:{user.id}", max_attempts=2)
    db.commit()
    from app.worker import run_pending_jobs, worker_online
    online = worker_online(db)
    if job is not None and not online:   # nobody is draining the queue: do it here, after the response
        background.add_task(run_pending_jobs, 50, 5.0)
    return {"queued": job is not None, "processing": "worker" if online else "in-process (no worker heartbeat found)"}
