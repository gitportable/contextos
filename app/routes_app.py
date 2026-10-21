"""Product API: Today, projects, assistant, reply drafts, and the live Data Pipeline page."""
import json
import re
import time
from collections import Counter
from datetime import datetime, time as dtime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from app import cache, integrations, llm, projects as projects_mod, ratelimit, summaries
from app.auth import admin_user, current_user, is_admin
from app.config import settings
from app.database import get_db
from app.models import (AssistantNote, PipelineEvent, ProcessingJob, Project, Reminder, SlackConnection, SlackMessage, SuggestedEvent, Summary,
                        User, UserSession, WorkerHeartbeat, as_utc, utcnow)
from app.priority import REQUEST_RE, get_tz
from app.schemas import AssistantQuery
from app.views import inbox, message_dicts, own_message

router = APIRouter(tags=["app"])


def err(status, code, message, **extra):
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def limit_or_429(user: User, name: str, limit: int, window: int = 60):
    ok, wait = ratelimit.hit(f"api:{name}:{user.id}", limit, window)
    if not ok:
        raise err(429, "ratelimited", f"Too many requests. Try again in {wait}s.", retry_after=wait)


# ------------------------------------------------------------------ today ---
def compute_today(db: Session, user: User) -> dict:
    now, tz = utcnow(), get_tz()
    local = now.astimezone(tz)
    end_today = datetime.combine(local.date() + timedelta(days=1), dtime(0), tzinfo=tz).astimezone(timezone.utc)
    base = inbox(db, user).filter(SlackMessage.handled_at.is_(None), (SlackMessage.snoozed_until.is_(None)) | (SlackMessage.snoozed_until <= now))
    urgent = (base.filter((SlackMessage.priority_label == "P0") | ((SlackMessage.priority_label == "P1") & SlackMessage.mentions_me.is_(True)
                                                                    & (SlackMessage.message_created_at >= now - timedelta(hours=48))))
              .order_by(SlackMessage.priority_label, desc(SlackMessage.message_created_at)).limit(8).all())
    rems = (db.query(Reminder).filter(Reminder.owner_id == user.id, Reminder.status == "pending", Reminder.due_at < end_today,
                                      Reminder.due_at > now - timedelta(days=3)).order_by(Reminder.due_at).limit(10).all())
    start_today = datetime.combine(local.date(), dtime(0), tzinfo=tz).astimezone(timezone.utc)
    ev_rows = (db.query(SuggestedEvent).outerjoin(SlackMessage, SuggestedEvent.message_id == SlackMessage.id)
           .filter(SuggestedEvent.owner_id == user.id, SuggestedEvent.status == "suggested",
                   or_(SuggestedEvent.proposed_start > now - timedelta(hours=1),
                       and_(SlackMessage.owner_id == user.id, SlackMessage.message_created_at >= start_today)))
           .order_by(SuggestedEvent.proposed_start).limit(100).all())
    evs, event_keys = [], set()
    for event in ev_rows:
        key = (event.title.casefold(), as_utc(event.proposed_start).replace(second=0, microsecond=0))
        if key not in event_keys:
            event_keys.add(key)
            evs.append(event)
    evs = evs[:16]
    event_message_ids = {event.message_id for event in evs if event.message_id is not None}
    event_priorities = {
        message_id: (label, score)
        for message_id, label, score in db.query(SlackMessage.id, SlackMessage.priority_label, SlackMessage.priority_score)
        .filter(SlackMessage.owner_id == user.id, SlackMessage.id.in_(event_message_ids)).all()
    } if event_message_ids else {}
    waiting = []
    for m in (db.query(SlackMessage).filter(SlackMessage.owner_id == user.id, SlackMessage.is_self.is_(True), SlackMessage.is_deleted.is_(False),
                                            SlackMessage.handled_at.is_(None), SlackMessage.message_created_at < now - timedelta(hours=2),
                                            SlackMessage.message_created_at > now - timedelta(days=7)).order_by(desc(SlackMessage.message_created_at)).limit(40).all()):
        if not (REQUEST_RE.search(m.text) or "?" in m.text):
            continue
        replied = db.query(SlackMessage.id).filter(SlackMessage.connection_id == m.connection_id, SlackMessage.channel_id == m.channel_id, SlackMessage.is_self.is_(False),
                                                   SlackMessage.is_deleted.is_(False), SlackMessage.thread_ts == m.slack_ts).first()
        if not replied and m.conv_type in ("im", "mpim"):
            replied = db.query(SlackMessage.id).filter(SlackMessage.connection_id == m.connection_id, SlackMessage.channel_id == m.channel_id, SlackMessage.is_self.is_(False),
                                                       SlackMessage.is_deleted.is_(False), SlackMessage.message_created_at > m.message_created_at).first()
        if not replied:
            waiting.append(m)
        if len(waiting) >= 8:
            break
    cards = {d["id"]: d for d in message_dicts(db, urgent + waiting)}
    def card(m, kind): return {**cards[m.id], "kind": kind}
    due_events, schedule_events = [], []
    for event in evs:
        start = as_utc(event.proposed_start)
        item = {"id": event.id, "kind": "event", "text": event.title, "proposed_start": event.proposed_start.isoformat(),
                "due_at": event.proposed_start.isoformat(), "provisional": event.is_provisional, "message_id": event.message_id,
                "priority_label": event_priorities.get(event.message_id, (None, None))[0],
                "priority_score": event_priorities.get(event.message_id, (None, None))[1]}
        (due_events if start_today <= start < end_today else schedule_events).append(item)
    return {
        "date": local.strftime("%A, %d %B"),
        "needs_now": [card(m, "message") for m in urgent],
        "due_today": ([{"id": r.id, "kind": "reminder", "text": r.title, "due_at": r.due_at.isoformat(), "message_id": r.message_id, "overdue": as_utc(r.due_at) < now} for r in rems]
                      + due_events),
        "to_schedule": schedule_events,
        "waiting_on_others": [card(m, "message") for m in waiting],
    }


@router.get("/api/today")
def today(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return compute_today(db, user)


@router.get("/api/briefing")
def briefing(refresh: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if refresh:
        limit_or_429(user, "briefing", 6)
    return summaries.daily_briefing(db, user, compute_today(db, user), refresh)


@router.post("/messages/{message_id}/handled")
def mark_handled(message_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = own_message(db, user, message_id)
    row.handled_at = utcnow(); db.commit(); cache.invalidate_user_caches(user.id)
    return {"ok": True}


class SnoozeIn(BaseModel):
    hours: int = Field(default=4, ge=1, le=168)


@router.post("/messages/{message_id}/snooze")
def snooze(message_id: int, data: SnoozeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = own_message(db, user, message_id)
    row.snoozed_until = utcnow() + timedelta(hours=data.hours); db.commit(); cache.invalidate_user_caches(user.id)
    return {"ok": True, "until": row.snoozed_until.isoformat()}


class SuggestIn(BaseModel):
    tone: str = Field(default="professional", max_length=20)


@router.post("/messages/{message_id}/suggest-reply")
def suggest_reply(message_id: int, data: SuggestIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    limit_or_429(user, "suggest", 20)
    return summaries.suggest_reply(db, user, own_message(db, user, message_id), data.tone)


# --------------------------------------------------------------- assistant ---
@router.post("/assistant/ask")
def assistant_ask(data: AssistantQuery, user: User = Depends(current_user), db: Session = Depends(get_db)):
    limit_or_429(user, "ask", 12)
    result = summaries.answer_question(db, user, data.question.strip()[:1000])
    db.add(AssistantNote(owner_id=user.id, question=data.question[:1000], answer=result["answer"][:6000],
                         source_message_ids=",".join(str(s["message_id"]) for s in result["sources"])))
    db.commit()
    return result


# ---------------------------------------------------------------- projects ---
def _project_dict(p: Project) -> dict:
    return {"id": p.id, "name": p.name, "keywords": [k for k in p.keywords.split(",") if k], "status": p.status, "message_count": p.message_count,
            "p0_count": p.p0_count, "first_seen_at": p.first_seen_at.isoformat() if p.first_seen_at else None,
            "last_activity_at": p.last_activity_at.isoformat() if p.last_activity_at else None, "summary": p.summary_text,
            "summary_at": p.summary_at.isoformat() if p.summary_at else None, "summary_source": p.summary_source}


def _own_project(db, user, pid) -> Project:
    p = db.query(Project).filter_by(id=pid, owner_id=user.id).first()
    if not p:
        raise err(404, "not_found", "Project not found.")
    return p


def _project_keywords(raw: str) -> list[str]:
    if len(raw) > 500:
        raise err(400, "invalid_keywords", "Keywords must be 500 characters or fewer.")
    keywords = list(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))
    if not keywords or len(keywords) > 8 or any(len(word) < 2 or len(word) > 60 or not re.search(r"[a-z0-9]", word) for word in keywords):
        raise err(400, "invalid_keywords", "Enter 1-8 keywords, each between 2 and 60 characters, separated by commas.")
    return keywords


@router.get("/api/projects")
def list_projects(include_archived: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = db.query(Project).filter(Project.owner_id == user.id)
    if not include_archived:
        q = q.filter(Project.status == "active")
    mix = {}
    for pid, label, n in (db.query(SlackMessage.project_id, SlackMessage.priority_label, func.count(SlackMessage.id))
                          .filter(SlackMessage.owner_id == user.id, SlackMessage.is_deleted.is_(False), SlackMessage.project_id.isnot(None))
                          .group_by(SlackMessage.project_id, SlackMessage.priority_label).all()):
        mix.setdefault(pid, {})[label] = n
    return [{**_project_dict(p), "priorities": mix.get(p.id, {})} for p in q.order_by(desc(Project.message_count), desc(Project.last_activity_at)).limit(100).all()]


@router.get("/api/projects/matches")
def project_matches(keywords: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    limit_or_429(user, "project_search", 30)
    terms = _project_keywords(keywords)
    rows = projects_mod.matching_messages(db, user.id, terms)
    return {
        "count": len(rows),
        "messages": [{"id": row.id, "text": (row.text or "")[:240],
                      "created_at": row.message_created_at.isoformat() if row.message_created_at else None,
                      "priority_label": row.priority_label} for row in rows[:5]],
    }


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    keywords: str = Field(min_length=2, max_length=500)


@router.post("/api/projects")
def create_project(data: ProjectCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    limit_or_429(user, "project_create", 10)
    name = data.name.strip()
    if not name or not re.search(r"[a-z0-9]", name, re.I):
        raise err(400, "invalid_name", "Enter a project name.")
    terms = _project_keywords(data.keywords)
    project, changed, matched = projects_mod.create_manual(db, user.id, name, terms)
    cache.invalidate_user_caches(user.id)
    return {**_project_dict(project), "tagged": changed, "matched": matched}


@router.get("/api/projects/{project_id}")
def project_detail(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own_project(db, user, project_id)
    base = db.query(SlackMessage).filter(SlackMessage.owner_id == user.id, SlackMessage.project_id == p.id, SlackMessage.is_deleted.is_(False))
    graph = integrations.project_graph(user.id, p.id)
    if graph is None:
        people = base.with_entities(func.coalesce(SlackMessage.author_name, SlackMessage.user_id), func.count(SlackMessage.id)).group_by(func.coalesce(SlackMessage.author_name, SlackMessage.user_id)).order_by(desc(func.count(SlackMessage.id))).limit(8).all()
        chans = base.with_entities(SlackMessage.channel_id, func.count(SlackMessage.id)).group_by(SlackMessage.channel_id).order_by(desc(func.count(SlackMessage.id))).limit(6).all()
        graph = {"people": [{"name": n or "unknown", "n": c} for n, c in people], "channels": [{"channel": c, "n": n} for c, n in chans], "source": "sql"}
    words = Counter()
    for (t,) in base.with_entities(SlackMessage.text).order_by(desc(SlackMessage.id)).limit(400).all():
        words.update(w for w in re.findall(r"[a-z][a-z-]{3,}", t.lower()) if w not in projects_mod.STOP and w not in p.keywords)
    since = utcnow() - timedelta(days=14)
    per_day = Counter()
    for (d,) in base.with_entities(SlackMessage.message_created_at).filter(SlackMessage.message_created_at >= since).all():
        per_day[as_utc(d).strftime("%Y-%m-%d")] += 1
    recent = base.order_by(desc(SlackMessage.priority_score), desc(SlackMessage.message_created_at)).limit(15).all()
    return {**_project_dict(p), "topics": [w for w, _ in words.most_common(8)], "graph": graph,
            "timeline": [{"day": (since + timedelta(days=i + 1)).strftime("%Y-%m-%d"), "n": per_day.get((since + timedelta(days=i + 1)).strftime("%Y-%m-%d"), 0)} for i in range(14)],
            "messages": message_dicts(db, recent)}


@router.post("/api/projects/{project_id}/summarize")
def project_summarize(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    limit_or_429(user, "summarize", 6)
    p = _own_project(db, user, project_id)
    row = summaries.summarize_project(db, p)
    if not row:
        raise err(409, "no_messages", "This project has no messages to summarise yet.")
    return _project_dict(p)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    status: str | None = Field(default=None, pattern="^(active|archived)$")
    add_keyword: str | None = Field(default=None, min_length=2, max_length=60)


@router.patch("/api/projects/{project_id}")
def project_patch(project_id: int, data: ProjectPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own_project(db, user, project_id)
    if data.name:
        p.name, p.renamed = data.name.strip(), True
    if data.status:
        p.status = data.status
    if data.add_keyword:
        p.keywords = ",".join(sorted(set(p.keywords.split(",")) | {data.add_keyword.strip().lower()} - {""}))
    db.commit(); projects_mod.invalidate(user.id)
    return _project_dict(p)


@router.post("/api/projects/discover")
def projects_discover(user: User = Depends(current_user), db: Session = Depends(get_db)):
    limit_or_429(user, "discover", 4)
    res = projects_mod.discover(db, user.id)
    res.pop("changed_ids", None)
    cache.invalidate_user_caches(user.id)
    return res


# ------------------------------------------------------------ data pipeline ---
_svc: dict = {"t": 0.0, "v": {}}


def service_status() -> dict:
    """Pings are cached for 10 s so the live page can poll without hammering Redis/Qdrant/Neo4j."""
    if time.time() - _svc["t"] < 10:
        return _svc["v"]
    out = {}
    r = cache.client() if settings.redis_url else None
    out["redis"] = ("ok", "connected") if r else (("down", "configured but unreachable") if settings.redis_url else ("off", "not configured"))
    if settings.qdrant_enabled:
        try:
            out["qdrant"] = ("ok", f"{len(integrations._qdrant().get_collections().collections)} collection(s)")
        except Exception as exc:
            out["qdrant"] = ("down", type(exc).__name__)
    else:
        out["qdrant"] = ("off", "not configured")
    if settings.neo4j_enabled:
        try:
            integrations._neo4j_driver().verify_connectivity(); out["neo4j"] = ("ok", "connected")
        except Exception as exc:
            out["neo4j"] = ("down", type(exc).__name__)
    else:
        out["neo4j"] = ("off", "not configured")
    _svc.update(t=time.time(), v=out)
    return out


def _layer(key, name, status, headline, **metrics):
    return {"key": key, "name": name, "status": status, "headline": headline, "metrics": metrics}


@router.get("/api/pipeline/overview")
def pipeline_overview(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    now, day = utcnow(), utcnow() - timedelta(hours=24)
    conns = db.query(SlackConnection).all()
    def evc(stage, status=None):
        q = db.query(func.count(PipelineEvent.id)).filter(PipelineEvent.stage == stage, PipelineEvent.created_at >= day)
        return (q.filter(PipelineEvent.status == status) if status else q).scalar() or 0
    msgs = db.query(SlackMessage).filter(SlackMessage.is_deleted.is_(False))
    total = msgs.count()
    tagged = msgs.filter(SlackMessage.project_id.isnot(None)).count()
    indexed = msgs.filter(SlackMessage.indexed_at.isnot(None)).count()
    p3 = msgs.filter(SlackMessage.priority_label == "P3", SlackMessage.project_id.is_(None)).count()
    prios = {l: c for l, c in msgs.with_entities(SlackMessage.priority_label, func.count(SlackMessage.id)).group_by(SlackMessage.priority_label).all()}
    jq = db.query(ProcessingJob)
    jobs = {s: c for s, c in jq.with_entities(ProcessingJob.status, func.count(ProcessingJob.id)).group_by(ProcessingJob.status).all()}
    oldest = jq.filter(ProcessingJob.status == "queued").with_entities(func.min(ProcessingJob.created_at)).scalar()
    hb = db.get(WorkerHeartbeat, "worker-1")
    age = (now - as_utc(hb.last_seen)).total_seconds() if hb else None
    svc = service_status()
    lu = llm.usage()
    nsum = db.query(func.count(Summary.id)).scalar() or 0
    last_sum = db.query(func.max(Summary.created_at)).scalar()
    top = db.query(Project).filter_by(status="active").order_by(desc(Project.message_count)).limit(3).all()
    connected = [c for c in conns if c.status == "connected"]
    bad = [c for c in conns if c.status in ("needs_reauth", "revoked")]
    stt = lambda s: {"ok": "ok", "off": "idle", "down": "down"}[s[0]]
    layers = [
        _layer("auth", "1 · Sign-in", "ok", "Authenticated session active", active_sessions=db.query(func.count(UserSession.id)).scalar(), google_linked=db.query(func.count(User.id)).filter(User.google_sub.isnot(None)).scalar(),
               users=db.query(func.count(User.id)).scalar(),
               signins_24h=evc("auth", "ok"), failed_attempts_24h=evc("auth", "error")),
        _layer("slack_oauth", "2 · Slack connection", "down" if bad else "ok" if connected else "idle",
               f"{len(connected)} connected" + (f", {len(bad)} need reconnecting" if bad else ""), workspaces=[c.team_name or c.team_id for c in conns],
               permissions=sorted({s for c in connected for s in c.scopes('user')})),
        _layer("ingress", "3 · Ingress (events + history sync)", "ok" if (evc("event_received") or evc("sync")) else "idle", f"{evc('event_received', 'ok')} events accepted, {evc('event_received', 'duplicate')} replays dropped (24h)",
               syncs_24h=evc("sync"), events_accepted=evc("event_received", "ok"), replays_dropped=evc("event_received", "duplicate")),
        _layer("queue", "4 · Durable queue (PostgreSQL)", "warn" if jobs.get("failed") else "ok", f"{jobs.get('queued', 0)} queued · {jobs.get('processing', 0)} running · {jobs.get('failed', 0)} failed",
               by_status=jobs, oldest_queued_s=int((now - as_utc(oldest)).total_seconds()) if oldest else 0),
        _layer("worker", "5 · Worker", "down" if age is None or age > 30 else "ok", "alive" if age is not None and age <= 30 else "NOT running: start `python -m app.worker`",
               last_seen_s=int(age) if age is not None else None, jobs_done=hb.jobs_done if hb else 0),
        _layer("ingest", "6 · Normalise + de-duplicate + store", "ok" if total else "idle", f"{total} messages stored", total=total, dedupes_24h=evc("ingest", "duplicate"), stored_24h=evc("ingest", "ok")),
        _layer("score", "7 · Priority scoring", "ok" if total else "idle", " · ".join(f"{k} {prios.get(k, 0)}" for k in ("P0", "P1", "P2", "P3")), **{k: prios.get(k, 0) for k in ("P0", "P1", "P2", "P3")}),
        _layer("extract", "8 · Reminders + meeting extraction", "ok", f"{db.query(func.count(Reminder.id)).filter_by(status='pending').scalar()} reminders · {db.query(func.count(SuggestedEvent.id)).filter_by(status='suggested').scalar()} meeting suggestions"),
        _layer("projects", "9 · Project discovery", "ok" if top else "idle", f"{db.query(func.count(Project.id)).filter_by(status='active').scalar()} projects · {tagged}/{total} messages tagged",
               top=[{"name": p.name, "messages": p.message_count} for p in top]),
        _layer("tiering", "10 · Storage tiering + retention", "ok", f"{indexed} embedded · {p3} low-value kept in SQL only", embedded=indexed, sql_only_low_value=p3,
               p3_expires_days=settings.retain_p3_days, all_expire_days=settings.retain_all_days),
        _layer("qdrant", "11 · Qdrant (semantic search)", stt(svc["qdrant"]), svc["qdrant"][1], embedded_messages=indexed),
        _layer("neo4j", "12 · Neo4j (people / project graph)", stt(svc["neo4j"]), svc["neo4j"][1], note="answers 'who works on project X, in which channels'"),
        _layer("redis", "13 · Redis (cache, rate limits)", stt(svc["redis"]), svc["redis"][1], rate_limit_backend="redis" if svc["redis"][0] == "ok" else "postgres"),
        _layer("llm", "14 · LLM (free tier)", "ok" if llm.providers() else "idle", ", ".join(llm.providers()) + " configured" if llm.providers() else "no key: extractive mode",
               usage=lu, fallbacks_24h=evc("llm", "skipped")),
        _layer("summaries", "15 · Summaries store", "ok" if nsum else "idle", f"{nsum} summaries" + (f" · last {as_utc(last_sum).strftime('%H:%M')}" if last_sum else ""), summaries=nsum),
    ]
    return {"layers": layers, "generated_at": now.isoformat()}


def _ev(e):
    return {"id": e.id, "stage": e.stage, "status": e.status, "detail": e.detail, "message_id": e.message_id, "at": e.created_at.isoformat()}


@router.get("/api/pipeline/events")
def pipeline_events(after_id: int = 0, limit: int = 60, stage: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    q = db.query(PipelineEvent)
    if stage:
        q = q.filter(PipelineEvent.stage == stage)
    if after_id:
        q = q.filter(PipelineEvent.id > after_id)
    return [_ev(e) for e in q.order_by(desc(PipelineEvent.id)).limit(max(1, min(limit, 200))).all()]


@router.get("/api/pipeline/trace/{message_id}")
def pipeline_trace(message_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    message = db.query(SlackMessage).filter_by(id=message_id).first() if is_admin(user) else own_message(db, user, message_id)
    if not message or message.is_deleted:
        raise err(404, "not_found", "Message not found.")
    rows_q = db.query(PipelineEvent).filter_by(message_id=message_id)
    if not is_admin(user):
        rows_q = rows_q.filter_by(owner_id=user.id)
    rows = rows_q.order_by(PipelineEvent.id).all()
    return {"message": message_dicts(db, [message])[0], "events": [_ev(e) for e in rows]}


@router.get("/api/admin/settings")
def admin_settings(user: User = Depends(admin_user)):
    return {
        "admin_email": settings.admin_email,
        "app_env": settings.app_env,
        "public_base_url": settings.public_base_url,
        "slack_oauth_configured": settings.slack_oauth_configured,
        "slack_redirect_uri": settings.redirect_uri,
        "google_enabled": settings.google_enabled,
        "redis_configured": bool(settings.redis_url),
        "qdrant_configured": settings.qdrant_enabled,
        "neo4j_configured": settings.neo4j_enabled,
        "llm_providers": llm.providers(),
    }
