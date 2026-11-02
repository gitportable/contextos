"""Features that exist BECAUSE of Qdrant and Neo4j (each has a PostgreSQL fallback so the app still works without them).

Qdrant (meaning)                                          Neo4j (relationships)
  smart_search      find messages by meaning, not words      people_overview   who is active across how many projects
  related_messages  "more like this", across Slack + Gmail   person_detail     which projects, who they work with
  merge_suggestions two projects whose summaries match       project_risks     "only one person writes about this"
  semantic_escalation  a quiet message that echoes an        (+ who-to-ask, related projects and the collaborator
                       urgent one is raised a little           boost, which live in assistant.py / projects.py)

Tenant isolation is unchanged: every vector query filters on owner_id, every Cypher query is constrained by owner, and every
id that comes back is re-loaded from PostgreSQL filtered by owner before anything is shown.
"""
import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import integrations, pipeline_events, summaries
from app.config import settings
from app.models import MessageProject, Project, SlackConnection, SlackMessage, User, utcnow
from app.projects import STOP
from app.serializers import message_dicts

log = logging.getLogger("contextos.intelligence")
RRF_K = 60


def _terms(q: str) -> list[str]:
    import re
    return [t for t in re.findall(r"[a-z0-9][a-z0-9\-_]{2,}", q.lower()) if t not in STOP][:6]


def _live(db: Session, owner_id: int, priority: str | None = None, source: str | None = None, include_self: bool = False):
    q = db.query(SlackMessage).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False))
    if not include_self:
        q = q.filter(SlackMessage.is_self.is_(False))
    if priority and priority.lower() == "urgent":
        q = q.filter(SlackMessage.priority_label.in_(("P0", "P1")))
    elif priority and priority.lower() == "high":
        q = q.filter(SlackMessage.priority_label == "P1")
    elif priority and priority.lower() == "normal":
        q = q.filter(SlackMessage.priority_label.in_(("P2", "P3")))
    elif priority and priority.lower() == "important":
        q = q.filter(SlackMessage.priority_label.in_(("P0", "P1", "P2", "P3")))
    elif priority and priority.upper() in ("P0", "P1", "P2", "P3"):
        q = q.filter(SlackMessage.priority_label == priority.upper())
    if source in ("slack", "gmail"):
        q = q.join(SlackConnection, SlackConnection.id == SlackMessage.connection_id).filter(SlackConnection.provider == source)
    return q


# ------------------------------------------------------------------ search ---
def smart_search(db: Session, user: User, q: str, limit: int = 30, priority: str | None = None, source: str | None = None) -> dict:
    """Keyword hits + hits by meaning (Qdrant), fused with reciprocal-rank fusion. Each result says how it matched."""
    q = (q or "").strip()[:300]
    if not q:
        return {"results": [], "semantic": "off" if not settings.qdrant_enabled else "idle", "query": q}
    limit = max(1, min(limit, 100))
    uid = user.id
    # 1) keywords
    esc = lambda t: t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")  # noqa: E731
    terms = _terms(q) or [q.lower()]
    conds = [SlackMessage.text.ilike(f"%{esc(t)}%", escape="\\") for t in terms]
    kw_rows = (_live(db, uid, priority, source, include_self=True).filter(or_(*conds)).order_by(SlackMessage.priority_score.desc(), SlackMessage.message_created_at.desc()).limit(300).all())
    kw_scored = sorted(((sum(1 for t in terms if t in (r.text or "").lower()), r) for r in kw_rows), key=lambda x: (x[0], x[1].priority_score), reverse=True)
    kw_rank = {r.id: i for i, (_, r) in enumerate(kw_scored)}
    # 2) meaning
    sem_hits: list[dict] = []
    state = "off"
    if settings.qdrant_enabled:
        sem_hits = integrations.search_vectors(q, uid, limit=limit * 2, min_score=settings.semantic_search_min_score)
        state = "on" if sem_hits else "no_hits"
    sem_rank = {h["message_id"]: i for i, h in enumerate(sem_hits) if h.get("message_id")}
    sem_score = {h["message_id"]: h["score"] for h in sem_hits if h.get("message_id")}
    ids = set(kw_rank) | set(sem_rank)
    if not ids:
        return {"results": [], "semantic": state, "query": q}
    rows = {r.id: r for r in _live(db, uid, priority, source, include_self=True).filter(SlackMessage.id.in_(list(ids))).all()}   # owner-scoped re-load
    fused = []
    for mid, r in rows.items():
        s = (1 / (RRF_K + kw_rank[mid]) if mid in kw_rank else 0) + (1 / (RRF_K + sem_rank[mid]) if mid in sem_rank else 0)
        fused.append((s, r, "both" if mid in kw_rank and mid in sem_rank else "words" if mid in kw_rank else "meaning"))
    fused.sort(key=lambda x: (x[0], x[1].priority_score), reverse=True)
    top = fused[:limit]
    dicts = {d["id"]: d for d in message_dicts(db, [r for _, r, _ in top])}
    out = []
    for s, r, how in top:
        d = dict(dicts[r.id])
        d["match"] = how
        d["similarity"] = round(sem_score[r.id], 3) if r.id in sem_score else None
        out.append(d)
    return {"results": out, "semantic": state, "query": q}


# ----------------------------------------------------------------- related ---
def related_messages(db: Session, user: User, message: SlackMessage, limit: int = 6) -> dict:
    """'More like this': nearest messages by meaning (Qdrant); the same project's messages when Qdrant is off."""
    uid = user.id
    provider = db.query(SlackConnection.provider).filter(SlackConnection.id == message.connection_id).scalar() or "slack"
    ids: list[int] = []
    scores: dict[int, float] = {}
    source = "postgresql"
    if settings.qdrant_enabled and message.text:
        hits = integrations.search_vectors(message.text[:500], uid, limit=limit + 4, exclude_ids=[message.id], min_score=settings.related_min_score)
        ids = [h["message_id"] for h in hits if h.get("message_id")]
        scores = {h["message_id"]: h["score"] for h in hits if h.get("message_id")}
        source = "qdrant"
    rows = []
    if ids:
        by_id = {r.id: r for r in _live(db, uid).filter(SlackMessage.id.in_(ids)).all()}
        rows = [by_id[i] for i in ids if i in by_id][:limit]
    if not rows:  # fallback (or Qdrant had nothing): other messages in the same project(s)
        pids = [p for (p,) in db.query(MessageProject.project_id).filter_by(message_id=message.id, owner_id=uid).all()]
        if pids:
            rows = (_live(db, uid).join(MessageProject, MessageProject.message_id == SlackMessage.id)
                    .filter(MessageProject.project_id.in_(pids), SlackMessage.id != message.id, SlackMessage.priority_label != "P3")
                    .order_by(SlackMessage.priority_score.desc(), SlackMessage.message_created_at.desc()).limit(limit).all())
            if rows:
                source = "postgresql (same project)"
    dicts = message_dicts(db, rows)
    for d in dicts:
        d["similarity"] = round(scores[d["id"]], 3) if d["id"] in scores else None
        d["cross_app"] = d["provider"] != provider
    return {"message_id": message.id, "source": source, "related": dicts}


# ------------------------------------------------------- merge suggestions ---
def merge_suggestions(db: Session, user: User, limit: int = 6) -> dict:
    """Projects that look like the same thing. Qdrant compares the SUMMARY vectors; the fallback compares which messages they share."""
    uid = user.id
    active = {p.id: p for p in db.query(Project).filter(Project.owner_id == uid, Project.status.in_(("auto", "confirmed"))).all()}
    if len(active) < 2:
        return {"source": "n/a", "suggestions": []}
    pairs = integrations.similar_project_pairs(uid, settings.merge_suggest_min_score)
    source = "qdrant"
    if pairs is None:
        source = "postgresql"
        members: dict[int, set[int]] = defaultdict(set)
        for mid, pid in db.query(MessageProject.message_id, MessageProject.project_id).filter(MessageProject.owner_id == uid, MessageProject.project_id.in_(list(active))).limit(100000):
            members[pid].add(mid)
        pairs = []
        ids = sorted(members)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                small = min(len(members[a]), len(members[b]))
                if small >= 3:
                    overlap = len(members[a] & members[b]) / small
                    if overlap >= 0.5:
                        pairs.append((a, b, round(overlap, 3)))
        pairs.sort(key=lambda t: -t[2])
    out = []
    for a, b, score in pairs:
        pa, pb = active.get(a), active.get(b)
        if not pa or not pb:
            continue
        small, big = (pa, pb) if (pa.message_count, pa.id) <= (pb.message_count, pb.id) else (pb, pa)
        why = (f"Their summaries are {int(score * 100)}% similar in meaning." if source == "qdrant"
               else f"{int(score * 100)}% of the smaller project's messages are also in the other.")
        out.append({"from": {"id": small.id, "name": small.name, "messages": small.message_count},
                    "into": {"id": big.id, "name": big.name, "messages": big.message_count}, "score": score, "why": why})
        if len(out) >= limit:
            break
    return {"source": source, "suggestions": out}


# ------------------------------------------------------------------ people ---
def _self_names(db: Session, uid: int) -> set[str]:
    return {(n or "").strip().lower() for (n,) in db.query(SlackMessage.author_name).filter(SlackMessage.owner_id == uid, SlackMessage.is_self.is_(True)).distinct().all()}


def people_overview(db: Session, user: User, limit: int = 60) -> dict:
    uid = user.id
    mp, mm = settings.collaborator_min_projects, settings.collaborator_min_messages
    mine = _self_names(db, uid)
    g = integrations.graph_people(uid, limit * 2)
    source = "neo4j"
    merged: dict[str, dict] = {}
    if g is not None:
        for x in g:
            k = (x["name"] or "").strip().lower()
            if not k or k in mine:
                continue
            cur = merged.setdefault(k, {"name": x["name"], "messages": 0, "projects": 0, "last_active": None})
            cur["messages"] += x["messages"]
            cur["projects"] = max(cur["projects"], x["projects"])
            cur["last_active"] = max(filter(None, [cur["last_active"], x["last_active"]]), default=None)
    else:
        source = "postgresql"
        lname = func.lower(SlackMessage.author_name)
        rows = (db.query(lname, func.max(SlackMessage.author_name), func.count(func.distinct(SlackMessage.id)), func.count(func.distinct(MessageProject.project_id)),
                         func.max(SlackMessage.message_created_at))
                .outerjoin(MessageProject, MessageProject.message_id == SlackMessage.id)
                .filter(SlackMessage.owner_id == uid, SlackMessage.is_deleted.is_(False), SlackMessage.is_bot.is_(False), SlackMessage.is_self.is_(False),
                        SlackMessage.author_name.isnot(None))
                .group_by(lname).order_by(func.count(func.distinct(MessageProject.project_id)).desc(), func.count(func.distinct(SlackMessage.id)).desc()).limit(limit * 2).all())
        for k, n, msgs, projs, last in rows:
            if k not in mine:
                merged[k] = {"name": n, "messages": msgs, "projects": projs, "last_active": last.isoformat() if last else None}
    people = sorted(merged.values(), key=lambda x: (-x["projects"], -x["messages"]))[:limit]
    for p in people:
        p["connector"] = p["projects"] >= mp and p["messages"] >= mm
    return {"source": source, "people": people, "connector_rule": f"active in {mp}+ projects with {mm}+ messages"}


def person_detail(db: Session, user: User, name: str) -> dict:
    uid = user.id
    key = (name or "").strip().lower()[:200]
    projects = integrations.graph_person_projects(uid, key)
    pro_src = "neo4j"
    if projects is None:
        pro_src = "postgresql"
        rows = (db.query(Project.id, Project.name, func.count(SlackMessage.id)).join(MessageProject, MessageProject.project_id == Project.id)
                .join(SlackMessage, SlackMessage.id == MessageProject.message_id)
                .filter(Project.owner_id == uid, Project.status.in_(("auto", "confirmed")), SlackMessage.owner_id == uid, SlackMessage.is_deleted.is_(False),
                        func.lower(SlackMessage.author_name) == key).group_by(Project.id, Project.name).order_by(func.count(SlackMessage.id).desc()).limit(12).all())
        projects = [{"id": i, "name": n, "messages": c} for i, n, c in rows]
    merged: dict[int, dict] = {}
    for p in projects:
        cur = merged.setdefault(p["id"], dict(p, messages=0))
        cur["messages"] += p["messages"]
    projects = sorted(merged.values(), key=lambda x: -x["messages"])
    ww = integrations.graph_works_with(uid, key)
    ww_src = "neo4j"
    if ww is None:
        ww_src = "postgresql"
        pids = [p["id"] for p in projects]
        ww = []
        if pids:
            lname = func.lower(SlackMessage.author_name)
            rows = (db.query(func.max(SlackMessage.author_name), func.count(func.distinct(MessageProject.project_id)))
                    .join(MessageProject, MessageProject.message_id == SlackMessage.id)
                    .filter(SlackMessage.owner_id == uid, SlackMessage.is_deleted.is_(False), SlackMessage.is_bot.is_(False), SlackMessage.is_self.is_(False),
                            MessageProject.project_id.in_(pids), SlackMessage.author_name.isnot(None), lname != key)
                    .group_by(lname).order_by(func.count(func.distinct(MessageProject.project_id)).desc()).limit(8).all())
            ww = [{"name": n, "shared_projects": c} for n, c in rows]
    mine = _self_names(db, uid)
    seen: dict[str, dict] = {}
    for w in ww:
        k = (w["name"] or "").strip().lower()
        if k in mine:
            continue
        cur = seen.setdefault(k, dict(w, shared_projects=0))
        cur["shared_projects"] = max(cur["shared_projects"], w["shared_projects"])
    works_with = sorted(seen.values(), key=lambda x: -x["shared_projects"])[:8]
    recent = (_live(db, uid).filter(func.lower(SlackMessage.author_name) == key, SlackMessage.priority_label != "P3")
              .order_by(SlackMessage.message_created_at.desc()).limit(5).all())
    return {"name": name, "projects": projects, "projects_source": pro_src, "works_with": works_with, "works_with_source": ww_src,
            "recent": message_dicts(db, recent)}


# -------------------------------------------------------------------- risk ---
def project_risks(db: Session, user: User) -> dict[int, dict]:
    """project_id -> {person, share, messages} for projects where one person wrote most of the messages (single point of failure)."""
    uid = user.id
    rows = integrations.graph_project_authors(uid)
    if rows is None:
        lname = func.lower(SlackMessage.author_name)
        agg = (db.query(Project.id, Project.name, lname, func.max(SlackMessage.author_name), func.count(SlackMessage.id))
               .join(MessageProject, MessageProject.project_id == Project.id).join(SlackMessage, SlackMessage.id == MessageProject.message_id)
               .filter(Project.owner_id == uid, Project.status.in_(("auto", "confirmed")), SlackMessage.owner_id == uid, SlackMessage.is_deleted.is_(False),
                       SlackMessage.is_bot.is_(False), SlackMessage.is_self.is_(False), SlackMessage.author_name.isnot(None))
               .group_by(Project.id, Project.name, lname).all())
        by: dict[int, dict] = {}
        for pid, pname, _, nm, n in agg:
            by.setdefault(pid, {"id": pid, "project": pname, "authors": []})["authors"].append({"name": nm, "n": n})
        rows = list(by.values())
    mine = _self_names(db, uid)
    out: dict[int, dict] = {}
    for r in rows:
        folded: dict[str, dict] = {}
        for a in r["authors"]:
            k = (a["name"] or "").strip().lower()
            if k and k not in mine:
                cur = folded.setdefault(k, {"name": a["name"], "n": 0})
                cur["n"] += a["n"]
        total = sum(a["n"] for a in folded.values())
        if total < settings.dependency_min_messages or not folded:
            continue
        top = max(folded.values(), key=lambda a: a["n"])
        share = top["n"] / total
        if share >= settings.dependency_share:
            out[int(r["id"])] = {"person": top["name"], "share": round(share, 2), "messages": total, "people": len(folded)}
    return out


# ---------------------------------------------------- semantic escalation ---
def apply_semantic_escalation(db: Session, owner_id: int) -> dict:
    """A quiet (P2/P3) recent message that is very close in meaning to an urgent (P0/P1) one gets a small, capped, explained
    boost: 'similar to an urgent message'. Idempotent. Commits."""
    if not settings.qdrant_enabled:
        return {"raised": 0, "skipped": "qdrant off"}
    since = utcnow() - timedelta(days=settings.escalation_window_days)
    cands = (db.query(SlackMessage).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False), SlackMessage.is_bot.is_(False),
                                           SlackMessage.is_self.is_(False), SlackMessage.message_created_at >= since,
                                           SlackMessage.priority_label.in_(("P2", "P3")), SlackMessage.priority_score >= 15,
                                           ~SlackMessage.reason.like("%similar to an urgent%"))
             .order_by(SlackMessage.message_created_at.desc()).limit(150).all())
    if not cands:
        return {"raised": 0, "checked": 0}
    res = integrations.nearest_urgent([m.text[:500] for m in cands], owner_id, settings.escalation_min_score)
    if res is None:
        return {"raised": 0, "skipped": "qdrant unavailable or nothing indexed yet"}
    urgent_ids = {mid for _, mid, _ in res}
    live_urgent = {i for (i,) in db.query(SlackMessage.id).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False),
                                                                    SlackMessage.priority_label.in_(("P0", "P1")), SlackMessage.id.in_(urgent_ids or {0})).all()}
    n = 0
    for idx, mid, score in res:
        m = cands[idx]
        if mid == m.id or mid not in live_urgent:
            continue
        m.priority_score = min(64, m.priority_score + settings.escalation_boost)   # capped below P1: an echo is a hint, not an alarm
        m.priority_label = "P2" if m.priority_score >= 35 else "P3"
        m.reason = (m.reason + "; " if m.reason else "") + f"similar to an urgent message (#{mid}, {int(score * 100)}% alike)"
        n += 1
    pipeline_events.record(db, "semantic_boost", "ok" if n else "skipped", owner_id=owner_id,
                           detail=f"{n} quiet message(s) raised for echoing an urgent one" if n else f"{len(cands)} quiet message(s) checked; none close enough")
    db.commit()
    return {"raised": n, "checked": len(cands)}


# ---------------------------------------------------------------- backfill ---
def backfill(db: Session, owner_id: int, *, force_graph: bool = False, max_messages: int = 4000) -> dict:
    """Bring Qdrant and Neo4j up to date with PostgreSQL for ONE user. Safe to run any time (upserts / MERGE are idempotent).
    Covers: messages never embedded, project summaries, the whole graph when Neo4j has fewer messages than PostgreSQL, project links."""
    from app import projects as projects_mod
    out: dict = {"qdrant": "off", "neo4j": "off"}
    batch = max(8, settings.index_backfill_batch)
    live = db.query(SlackMessage).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False), SlackMessage.priority_label != "P3")
    if settings.qdrant_enabled:
        done, err = 0, None
        while done < max_messages:
            rows = live.filter(SlackMessage.indexed_at.is_(None)).order_by(SlackMessage.id.desc()).limit(batch).all()
            if not rows:
                break
            status = integrations.index_vectors(rows)
            if status != "indexed":
                err = status
                break
            now = utcnow()
            for r in rows:
                r.indexed_at = now
            db.commit()
            done += len(rows)
        n_sum = 0
        for p in db.query(Project).filter(Project.owner_id == owner_id, Project.status.in_(("auto", "confirmed"))).all():
            s = summaries.latest(db, p.id)
            if s and integrations.index_summary(owner_id, p.id, p.name, s.summary_text) == "indexed":
                n_sum += 1
        out["qdrant"] = err or "ok"
        out["vectors_added"], out["summaries_indexed"] = done, n_sum
    if settings.neo4j_enabled:
        stats = integrations.neo4j_stats(owner_id) or {}
        have = stats.get("messages", 0) if "error" not in stats else -1
        want = live.count()
        if have < 0:
            out["neo4j"] = f"error: {stats.get('error')}"
        elif force_graph or have < want:
            done, err, last = 0, None, 0
            while done < max_messages * 2:
                rows = live.filter(SlackMessage.id > last).order_by(SlackMessage.id.asc()).limit(batch * 4).all()
                if not rows:
                    break
                status = integrations.index_graph(rows)
                if status != "synced":
                    err = status
                    break
                last = rows[-1].id
                done += len(rows)
            out["neo4j"] = err or "ok"
            out["graph_messages_synced"] = done
        else:
            out["neo4j"] = "up to date"
    if settings.qdrant_enabled or settings.neo4j_enabled:
        out["links"] = projects_mod.sync_external_indexes(db, owner_id)
    return out
