"""Endpoints behind the live pipeline page. ADMIN ONLY (see auth.require_admin). Everything is scoped to the signed-in admin: you see your own sign-in, your
Slack connections, your messages' journeys, and the workspace-level 'received/queued' rows of workspaces you are connected to."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from app import cache
from app.auth import require_admin
from app.config import settings
from app.database import get_db
from app.models import (LlmUsage, MessageProject, PipelineEvent, ProcessingJob, Project, ProjectSummary, SlackConnection, SlackMessage, User,
                        WorkerHeartbeat, as_utc, utcnow)
from app.pipeline_events import LAYERS, STAGE_NAMES, TEAM_LEVEL_STAGES

router = APIRouter(tags=["pipeline"])


def _scope(db: Session, user: User):
    teams = [t for (t,) in db.query(SlackConnection.team_id).filter_by(owner_id=user.id).all()]
    cond = PipelineEvent.owner_id == user.id
    if teams:
        cond = or_(cond, and_(PipelineEvent.owner_id.is_(None), PipelineEvent.team_id.in_(teams), PipelineEvent.stage.in_(TEAM_LEVEL_STAGES)))
    return cond


def _ev(e: PipelineEvent) -> dict:
    return {"id": e.id, "stage": e.stage, "status": e.status, "trace_id": e.trace_id, "message_id": e.message_id, "detail": e.detail,
            "at": e.created_at.isoformat() if e.created_at else None}


@router.get("/api/pipeline/events")
def events(since_id: int = 0, limit: int = 80, stage: str | None = None, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Poll with since_id=<last id you saw> to stream. Without it you get the most recent rows."""
    limit = max(1, min(limit, 300))
    q = db.query(PipelineEvent).filter(_scope(db, user))
    if stage:
        if stage not in STAGE_NAMES and stage != "retention":
            raise HTTPException(status_code=400, detail={"code": "bad_stage", "message": "Unknown stage."})
        q = q.filter(PipelineEvent.stage == stage)
    if since_id > 0:
        rows = q.filter(PipelineEvent.id > since_id).order_by(PipelineEvent.id.asc()).limit(limit).all()
    else:
        rows = list(reversed(q.order_by(desc(PipelineEvent.id)).limit(limit).all()))
    return {"events": [_ev(r) for r in rows], "cursor": rows[-1].id if rows else since_id}


@router.get("/messages/{message_id}/journey")
def journey(message_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Every stage one message passed through, in order (the 'show me the pipeline working' view)."""
    m = db.query(SlackMessage).filter_by(id=message_id, owner_id=user.id).first()
    if not m:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Message not found."})
    conds = [PipelineEvent.message_id == m.id]
    if m.source_event_id:
        conds.append(PipelineEvent.trace_id == m.source_event_id)
    rows = db.query(PipelineEvent).filter(_scope(db, user), or_(*conds)).order_by(PipelineEvent.id.asc()).limit(60).all()
    proj = (db.query(Project.id, Project.name).join(MessageProject, MessageProject.project_id == Project.id)
            .filter(MessageProject.message_id == m.id, Project.owner_id == user.id).all())
    return {"message_id": m.id, "source": m.source, "events": [_ev(r) for r in rows], "projects": [{"id": i, "name": n} for i, n in proj],
            "note": None if rows else "No stage log for this message (history-synced messages are logged per page, not per message)."}


@router.get("/api/pipeline/overview")
def overview(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    now = utcnow()
    since = now - timedelta(hours=24)
    scope = _scope(db, user)
    stats: dict[str, dict] = {}
    for stage, status, n, last in (db.query(PipelineEvent.stage, PipelineEvent.status, func.count(PipelineEvent.id), func.max(PipelineEvent.id))
                                   .filter(scope, PipelineEvent.created_at >= since).group_by(PipelineEvent.stage, PipelineEvent.status).all()):
        st = stats.setdefault(stage, {"ok": 0, "skipped": 0, "error": 0, "last_id": 0})
        st[status if status in ("ok", "skipped", "error") else "ok"] += n
        st["last_id"] = max(st["last_id"], last or 0)
    last_ids = [v["last_id"] for v in stats.values() if v["last_id"]]
    last_rows = {e.id: e for e in db.query(PipelineEvent).filter(PipelineEvent.id.in_(last_ids)).all()} if last_ids else {}
    layers = []
    for stage, label, group in LAYERS:
        st = stats.get(stage, {"ok": 0, "skipped": 0, "error": 0, "last_id": 0})
        le = last_rows.get(st["last_id"])
        layers.append({"stage": stage, "label": label, "group": group, "ok": st["ok"], "skipped": st["skipped"], "error": st["error"],
                       "last_status": le.status if le else None, "last_detail": le.detail if le else None,
                       "last_at": le.created_at.isoformat() if le and le.created_at else None})
    conn_ids = [i for (i,) in db.query(SlackConnection.id).filter_by(owner_id=user.id).all()]
    teams = [t for (t,) in db.query(SlackConnection.team_id).filter_by(owner_id=user.id).all()]
    jq = db.query(ProcessingJob.status, func.count(ProcessingJob.id))
    conds = [ProcessingJob.owner_id == user.id]
    if conn_ids:
        conds.append(ProcessingJob.connection_id.in_(conn_ids))
    if teams:
        conds.append(ProcessingJob.team_id.in_(teams))
    jobs = {s: c for s, c in jq.filter(or_(*conds)).group_by(ProcessingJob.status).all()}
    hb = db.query(func.max(WorkerHeartbeat.beat_at), func.sum(WorkerHeartbeat.jobs_done), func.count(WorkerHeartbeat.worker_id)).one()
    beat = as_utc(hb[0])
    online = bool(beat and now - beat < timedelta(seconds=90))
    day = now.strftime("%Y-%m-%d")
    usage = db.query(LlmUsage).filter_by(owner_id=user.id, day=day).first()
    msgs = db.query(func.count(SlackMessage.id)).filter(SlackMessage.owner_id == user.id, SlackMessage.is_deleted.is_(False)).scalar() or 0
    tiers = {p: c for p, c in db.query(SlackMessage.priority_label, func.count(SlackMessage.id)).filter(
        SlackMessage.owner_id == user.id, SlackMessage.is_deleted.is_(False)).group_by(SlackMessage.priority_label).all()}
    return {
        "worker": {"online": online, "last_beat": beat.isoformat() if beat else None, "workers": int(hb[2] or 0), "jobs_done": int(hb[1] or 0),
                   "hint": None if online else "No worker heartbeat. Start `python -m app.worker` so queued work gets processed."},
        "queue": jobs, "layers": layers,
        "totals": {"messages": msgs, "by_priority": {p: tiers.get(p, 0) for p in ("P0", "P1", "P2", "P3")},
                   "projects": db.query(func.count(Project.id)).filter(Project.owner_id == user.id, Project.status.in_(("auto", "confirmed"))).scalar() or 0,
                   "summaries": db.query(func.count(ProjectSummary.id)).filter_by(owner_id=user.id).scalar() or 0},
        "llm": {"enabled": settings.llm_enabled, "model": settings.llm_model if settings.llm_enabled else None,
                "calls_today": usage.calls if usage else 0, "user_daily_cap": settings.llm_user_daily_cap, "rpm_limit": settings.llm_rpm},
        "services": {"redis": ("connected" if cache.client() else "unreachable") if settings.redis_url else "not configured",
                     "qdrant": "configured" if settings.qdrant_enabled else "not configured",
                     "neo4j": "configured" if settings.neo4j_enabled else "not configured"},
        "redis_progress": {"messages_synced": cache.progress_get(user.id, "messages_synced")},
    }


@router.get("/api/selfcheck")
def selfcheck(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """'Is every feature actually working for MY data?' Each check looks at real rows / real round-trips and returns
    ok | warn (feature on but nothing to show yet) | off (not configured) | fail. Load the sample data, then run this."""
    from app import integrations
    from app.models import UserSession
    checks: list[dict] = []

    def add(cid, label, status, detail):
        checks.append({"id": cid, "label": label, "status": status, "detail": detail})

    uid = user.id
    live = [SlackMessage.owner_id == uid, SlackMessage.is_deleted.is_(False)]
    n_slack = db.query(func.count(SlackMessage.id)).join(SlackConnection, SlackConnection.id == SlackMessage.connection_id).filter(*live, SlackConnection.provider == "slack").scalar() or 0
    n_mail = db.query(func.count(SlackMessage.id)).join(SlackConnection, SlackConnection.id == SlackMessage.connection_id).filter(*live, SlackConnection.provider == "gmail").scalar() or 0
    add("login", "Sign-in & session", "ok" if db.query(UserSession.id).filter_by(user_id=uid).first() else "warn", f"signed in as {user.email}"
        + (" via Google" if user.google_sub else ""))
    hb = db.query(func.max(WorkerHeartbeat.beat_at)).scalar()
    online = bool(as_utc(hb) and utcnow() - as_utc(hb) < timedelta(seconds=90))
    add("worker", "Background worker", "ok" if online else "fail", "heartbeat within 90s" if online else "no heartbeat: start `python -m app.worker`")
    add("slack", "Slack messages ingested", "ok" if n_slack else "warn", f"{n_slack} messages" if n_slack else "none yet: connect Slack or load sample data")
    add("gmail", "Gmail messages ingested", "ok" if n_mail else "warn", f"{n_mail} emails" if n_mail else "none yet: connect Gmail or load sample data")
    tiers = {p: c for p, c in db.query(SlackMessage.priority_label, func.count(SlackMessage.id)).filter(*live).group_by(SlackMessage.priority_label).all()}
    add("scoring", "Priority scoring", "ok" if len([t for t in tiers.values() if t]) >= 3 else "warn",
        " ".join(f"{p}={tiers.get(p, 0)}" for p in ("P0", "P1", "P2", "P3")))
    add("reminders", "Reminders & meeting ideas", "ok" if _count_artifacts(db, uid) else "warn", f"{_count_artifacts(db, uid)} extracted")
    projs = db.query(Project).filter(Project.owner_id == uid, Project.status.in_(("auto", "confirmed"))).all()
    add("projects", "Projects from keywords", "ok" if projs else "warn", ", ".join(p.name for p in projs[:6]) if projs else "none discovered yet")
    cross = 0
    for p in projs:
        provs = {x for (x,) in db.query(SlackConnection.provider).join(SlackMessage, SlackMessage.connection_id == SlackConnection.id)
                 .join(MessageProject, MessageProject.message_id == SlackMessage.id).filter(MessageProject.project_id == p.id).distinct()}
        cross += len(provs) > 1
    add("cross_app", "Projects spanning Slack + Gmail", "ok" if cross else "warn", f"{cross} project(s) contain both" if cross else "needs both sources connected")
    sums = db.query(ProjectSummary.mode, func.count(ProjectSummary.id)).filter_by(owner_id=uid).group_by(ProjectSummary.mode).all()
    mode_counts = dict(sums)
    add("summaries", "Project summaries", "ok" if mode_counts else "warn", ", ".join(f"{v} {k}" for k, v in mode_counts.items()) or "none yet")
    add("llm", "LLM (AI summaries & answers)", "ok" if settings.llm_enabled and mode_counts.get("llm") else "warn" if settings.llm_enabled else "off",
        f"{settings.llm_model}" if settings.llm_enabled else "LLM_API_KEY not set: using extractive fallback")
    cite_ok = db.query(func.count(ProjectSummary.id)).filter(ProjectSummary.owner_id == uid, ProjectSummary.key_points_json != "[]").scalar() or 0
    add("citations", "Summaries cite source messages", "ok" if cite_ok else "warn", f"{cite_ok} summaries with cited key points")
    qs = integrations.qdrant_stats(uid)
    if qs is None:
        add("qdrant", "Qdrant semantic index", "off", "QDRANT_URL not set")
    elif "error" in qs:
        add("qdrant", "Qdrant semantic index", "fail", qs["error"])
    else:
        add("qdrant", "Qdrant semantic index", "ok" if qs["messages"] and qs["summaries"] else "warn", f"{qs['messages']} message vectors, {qs['summaries']} summary vectors")
        sem = db.query(func.count(MessageProject.id)).filter(MessageProject.owner_id == uid, MessageProject.hits == 0).scalar() or 0
        add("semantic", "Semantic project links (by meaning)", "ok" if sem else "warn", f"{sem} messages linked with no keyword match" if sem else "none yet: needs summaries first")
    ns = integrations.neo4j_stats(uid)
    if ns is None:
        add("neo4j", "Neo4j relationship graph", "off", "NEO4J_URI not set")
    elif "error" in ns:
        add("neo4j", "Neo4j relationship graph", "fail", ns["error"])
    else:
        add("neo4j", "Neo4j relationship graph", "ok" if ns["links"] else "warn", f"{ns['people']} people, {ns['messages']} messages, {ns['projects']} projects, {ns['links']} links")
    echoed = db.query(func.count(SlackMessage.id)).filter(*live, SlackMessage.reason.like("%similar to an urgent%")).scalar() or 0
    if qs is not None and "error" not in qs:
        add("echo", "Urgency echo (Qdrant)", "ok" if echoed else "warn", f"{echoed} quiet message(s) raised" if echoed else "none yet (needs an urgent message and a similar quiet one)")
    boosted = db.query(func.count(SlackMessage.id)).filter(*live, SlackMessage.reason.like("%key collaborator%")).scalar() or 0
    add("collab", "Key-collaborator priority boost", "ok" if boosted else "warn", f"{boosted} message(s) raised" if boosted else "none yet (needs people active in 2+ projects)")
    add("redis", "Redis (rate limits / cache)", "ok" if cache.client() else "off", "connected" if cache.client() else "REDIS_URL not set: per-process limits")
    stages = {s for (s,) in db.query(PipelineEvent.stage).filter(PipelineEvent.owner_id == uid).distinct()}
    missing = [s for s in ("ingested", "scored", "project_linked", "summarized") if s not in stages]
    add("events", "Pipeline events recorded", "ok" if not missing else "warn", "all core stages seen" if not missing else "not seen yet: " + ", ".join(missing))
    order = {"fail": 0, "warn": 1, "ok": 2, "off": 3}
    return {"checks": checks, "summary": {k: sum(1 for c in checks if c["status"] == k) for k in order}}


def _count_artifacts(db: Session, uid: int) -> int:
    from app.models import Reminder, SuggestedEvent
    return (db.query(func.count(Reminder.id)).filter(Reminder.owner_id == uid).scalar() or 0) + (db.query(func.count(SuggestedEvent.id)).filter(SuggestedEvent.owner_id == uid).scalar() or 0)
