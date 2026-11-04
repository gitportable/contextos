"""Project endpoints. Every query is scoped to the signed-in user; a project id from another user is a plain 404."""
import re
import threading

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app import integrations, intelligence, projects, ratelimit, summaries
from app.auth import current_user
from app.config import settings
from app.database import get_db
from app.models import MessageProject, Project, ProjectKeyword, ProjectSummary, SlackConversation, SlackMessage, User
from app.schemas import ProjectCreate, ProjectMerge, ProjectPatch
from app.serializers import message_dicts

router = APIRouter(tags=["projects"])
KW_OK = re.compile(r"^[\w][\w\s\-]{1,58}[\w]$")


def _err(status: int, code: str, message: str):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _own(db: Session, user: User, project_id: int) -> Project:
    p = db.query(Project).filter_by(id=project_id, owner_id=user.id).first()
    if not p:
        raise _err(404, "not_found", "Project not found.")
    return p


def _keywords(raw: list[str]) -> list[str]:
    out = []
    for k in raw:
        k = projects.norm_key(k)
        if not KW_OK.match(k):
            raise _err(400, "bad_keyword", f"Keyword '{k[:40]}' must be 3-60 letters/numbers.")
        if k not in out:
            out.append(k)
    return out


def _slug(db: Session, owner_id: int, name: str, exclude_id: int | None = None) -> str:
    base, slug, i = projects.slugify(name), projects.slugify(name), 2
    while db.query(Project.id).filter(Project.owner_id == owner_id, Project.slug == slug, Project.id != (exclude_id or 0)).first():
        slug, i = f"{base}-{i}", i + 1
    return slug


def _card_data(p: Project, summary: ProjectSummary | None, keywords: list[str]) -> dict:
    return {"id": p.id, "name": p.name, "slug": p.slug, "status": p.status, "keywords": keywords, "message_count": p.message_count,
            "channel_count": p.channel_count, "author_count": p.author_count, "score": round(p.score, 2),
            "first_seen_at": p.first_seen_at.isoformat() if p.first_seen_at else None,
            "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
            "health": summary.status_label if summary else "unknown", "summary_mode": summary.mode if summary else None,
            "summary_preview": (summary.summary_text[:200] if summary else None)}

def _card(db: Session, p: Project) -> dict:
    """Detailed single-project serializer; list endpoints use _cards to batch related reads."""
    summary = summaries.latest(db, p.id)
    keywords = [k for (k,) in db.query(ProjectKeyword.keyword).filter_by(project_id=p.id)
                .order_by(ProjectKeyword.is_primary.desc(), ProjectKeyword.keyword).all()]
    return _card_data(p, summary, keywords)

def _cards(db: Session, projects_list: list[Project]) -> list[dict]:
    """Serialize a project list with three bounded queries instead of 1 + 2N queries."""
    if not projects_list:
        return []
    ids = [p.id for p in projects_list]
    latest_versions = (db.query(ProjectSummary.project_id, func.max(ProjectSummary.version).label("version"))
                       .filter(ProjectSummary.project_id.in_(ids))
                       .group_by(ProjectSummary.project_id).subquery())
    latest_rows = (db.query(ProjectSummary).join(latest_versions,
                    (ProjectSummary.project_id == latest_versions.c.project_id) &
                    (ProjectSummary.version == latest_versions.c.version)).all())
    summaries_by_id = {row.project_id: row for row in latest_rows}
    keyword_rows = (db.query(ProjectKeyword.project_id, ProjectKeyword.keyword)
                    .filter(ProjectKeyword.project_id.in_(ids))
                    .order_by(ProjectKeyword.is_primary.desc(), ProjectKeyword.keyword).all())
    keywords_by_id: dict[int, list[str]] = {}
    for project_id, keyword in keyword_rows:
        keywords_by_id.setdefault(project_id, []).append(keyword)
    return [_card_data(p, summaries_by_id.get(p.id), keywords_by_id.get(p.id, [])) for p in projects_list]


_drain_lock = threading.Lock()


def _drain_once(run) -> None:
    """One in-process drain at a time: refreshing the page ten times must not start ten competing runners."""
    if not _drain_lock.acquire(blocking=False):
        return
    try:
        run(200, 20.0)
    finally:
        _drain_lock.release()


def _run_queue_here(background: BackgroundTasks, db: Session) -> str:
    """Discovery and summaries are queue jobs. With no worker process running they would sit there forever and the
    Projects page would stay empty, so in that case drain the queue in this process, after the response is sent."""
    from app.worker import run_pending_jobs, worker_online
    if worker_online(db):
        return "worker"
    background.add_task(_drain_once, run_pending_jobs)
    return "in-process"


@router.get("/projects")
def list_projects(background: BackgroundTasks, include: str = "active", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = db.query(Project).filter(Project.owner_id == user.id)
    q = q if include == "all" else q.filter(Project.status.in_(("auto", "confirmed")))
    project_rows = q.order_by(desc(Project.message_count), desc(Project.score)).limit(100).all()
    risks = intelligence.project_risks(db, user) if project_rows else {}
    cards = [{**card, "risk": risks.get(card["id"])} for card in _cards(db, project_rows)]
    if not cards and include != "all":
        # First visit (or never discovered): look for projects now instead of showing an empty page and waiting for the scheduler.
        discovered = db.query(User.projects_discovered_at).filter_by(id=user.id).scalar()
        has_messages = db.query(SlackMessage.id).filter(SlackMessage.owner_id == user.id, SlackMessage.is_deleted.is_(False)).first() is not None
        if has_messages and discovered is None:
            job = projects.enqueue_discovery(db, user.id)
            db.commit()
            if job is not None:
                _run_queue_here(background, db)
    return cards


@router.get("/projects/{project_id}")
def project_detail(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own(db, user, project_id)
    people = integrations.graph_project_people(user.id, p.id)
    people_source = "neo4j" if people is not None else "postgresql"
    if people is None:
        people = projects.project_people(db, user.id, p.id)
    chans = (db.query(SlackConversation.name, func.count(SlackMessage.id))
             .join(SlackMessage, (SlackMessage.channel_id == SlackConversation.channel_id) & (SlackMessage.connection_id == SlackConversation.connection_id))
             .join(MessageProject, MessageProject.message_id == SlackMessage.id)
             .filter(MessageProject.project_id == p.id, SlackMessage.is_deleted.is_(False))
             .group_by(SlackConversation.name).order_by(func.count(SlackMessage.id).desc()).limit(6).all())
    recent = (db.query(SlackMessage).join(MessageProject, MessageProject.message_id == SlackMessage.id)
              .filter(MessageProject.project_id == p.id, SlackMessage.owner_id == user.id, SlackMessage.is_deleted.is_(False))
              .order_by(desc(SlackMessage.message_created_at)).limit(15).all())
    versions = db.query(ProjectSummary.version, ProjectSummary.mode, ProjectSummary.created_at).filter_by(project_id=p.id).order_by(desc(ProjectSummary.version)).limit(10).all()
    related, related_src = projects.related_projects(db, user.id, p.id)
    merged: dict[str, dict] = {}  # the same person on Slack and in email is one person
    for x in people or []:
        k = (x.get("name") or "").strip().lower()
        if k in merged:
            merged[k]["messages"] += x.get("messages", 0)
        else:
            merged[k] = dict(x)
    people = sorted(merged.values(), key=lambda x: -x.get("messages", 0))
    return {**_card(db, p), "risk": intelligence.project_risks(db, user).get(p.id), "summary": summaries.summary_dict(summaries.latest(db, p.id)), "people": people, "people_source": people_source, "related": related, "related_source": related_src,
            "channels": [{"name": n or "?", "messages": c} for n, c in chans], "recent_messages": message_dicts(db, recent),
            "summary_versions": [{"version": v, "mode": m, "created_at": c.isoformat()} for v, m, c in versions]}


@router.post("/projects")
def create_project(data: ProjectCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    name = data.name.strip()
    if db.query(Project).filter_by(owner_id=user.id).count() >= 200:
        raise _err(409, "too_many", "Project limit reached. Archive some first.")
    kws = _keywords(data.keywords + [name])
    p = Project(owner_id=user.id, name=name, slug=_slug(db, user.id, name), status="confirmed")
    db.add(p)
    db.flush()
    for i, k in enumerate(kws):
        db.add(ProjectKeyword(project_id=p.id, keyword=k, is_primary=(i == len(kws) - 1)))
    projects.relink_owner(db, user.id)
    db.commit()
    projects.enqueue_summary(db, p.id, force=True)
    db.commit()
    return _card(db, p)


@router.patch("/projects/{project_id}")
def patch_project(project_id: int, data: ProjectPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own(db, user, project_id)
    if data.name is not None:
        p.name, p.slug = data.name.strip(), _slug(db, user.id, data.name, p.id)
        p.renamed = True
        if p.status == "auto":
            p.status = "confirmed"  # the user touched it: discovery must not rename or retire it
    if data.keywords is not None:
        kws = _keywords(data.keywords)
        if not kws:
            raise _err(400, "bad_keyword", "A project needs at least one keyword.")
        db.query(ProjectKeyword).filter_by(project_id=p.id).delete(synchronize_session=False)
        for i, k in enumerate(kws):
            db.add(ProjectKeyword(project_id=p.id, keyword=k, is_primary=(i == 0)))
        if p.status == "auto":
            p.status = "confirmed"
    if data.status is not None:
        p.status = data.status
    p.summary_dirty = True
    db.flush()
    projects.relink_owner(db, user.id)
    db.commit()
    return _card(db, p)


@router.post("/projects/{project_id}/merge")
def merge_project(project_id: int, data: ProjectMerge, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src, dst = _own(db, user, project_id), _own(db, user, data.into_id)
    if src.id == dst.id:
        raise _err(400, "same_project", "Pick a different project to merge into.")
    have = {k for (k,) in db.query(ProjectKeyword.keyword).filter_by(project_id=dst.id).all()}
    for k in db.query(ProjectKeyword).filter_by(project_id=src.id).all():
        if k.keyword in have:
            db.delete(k)
        else:
            k.project_id, k.is_primary = dst.id, False
    src.status = "archived"
    if dst.status == "auto":
        dst.status = "confirmed"
    dst.summary_dirty = True
    db.flush()
    projects.relink_owner(db, user.id)
    db.commit()
    return _card(db, dst)


def _expensive(user: User) -> None:
    try:
        ratelimit.hit("expensive:user", str(user.id), settings.rl_expensive_user_per_hour, 3600)
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc


@router.post("/projects/discover")
def rediscover(background: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _expensive(user)
    job = projects.enqueue_discovery(db, user.id)
    db.commit()
    runner = _run_queue_here(background, db) if job is not None else "queued"
    return {"ok": True, "queued": job is not None, "processing": runner,
            "note": "Looking through your recent messages now. New projects appear here in a few seconds."}


@router.post("/projects/{project_id}/summarize")
def summarize_now(project_id: int, background: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own(db, user, project_id)
    _expensive(user)
    job = projects.enqueue_summary(db, p.id, force=True)
    db.commit()
    if job is not None:
        _run_queue_here(background, db)
    return {"ok": True, "queued": job is not None}


@router.get("/projects/{project_id}/summaries")
def summary_history(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own(db, user, project_id)
    rows = db.query(ProjectSummary).filter_by(project_id=p.id).order_by(desc(ProjectSummary.version)).limit(20).all()
    return [summaries.summary_dict(r) for r in rows]


@router.get("/api/awaiting-reply")
def awaiting_reply(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Who do I owe a reply to? Messages that mention or DM you, with nothing from you after them."""
    return message_dicts(db, projects.awaiting_reply(db, user.id))
