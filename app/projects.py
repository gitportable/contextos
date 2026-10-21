"""Project discovery from the keywords people actually use.

A "project" is a name that recurs across the user's Slack messages ("Project Atlas", "Orion launch", "#proj-helios").
Strong patterns (explicit project phrases) need only a few messages; weak signals (a capitalised word that keeps
appearing mid-sentence) need much more evidence. Messages are then tagged with the best matching project, replies inherit
their thread's project, and projects are ranked by how much they are discussed.
"""
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Project, SlackConnection, SlackMessage, SlackUser, utcnow
from app.trace import log_event

log = logging.getLogger("contextos.projects")

STOP = set("""a an the and or but if then so to of in on at by for from with without about into over under again also just very can could would should will
shall may might must do does did done have has had is are was were be been being am i you he she it we they me him her us them my your our their this that these
those there here what when where which who whom why how all any both each few more most other some such no nor not only own same than too s t don now please
thanks thank hi hello hey ok okay yes yeah let lets get got go going need needs want review update send share check confirm approve reply follow let's
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september october november december
today tomorrow tonight yesterday week weekly monthly daily quarterly annual morning evening team everyone folks guys fyi asap eod urgent critical
slack zoom google gmail meet meeting call sync standup friday's new next last first final one two three
project projects launch release rollout migration revamp redesign sprint roadmap initiative program rewrite audit upgrade integration overhaul
update status decision blocker fyi quick done thanks looks agreed""".split())
GENERIC_HEADS = {"product", "marketing", "sales", "engineering", "design", "data", "customer", "q1", "q2", "q3", "q4", "company", "board", "all"}
SUFFIXES = ("project", "launch", "release", "rollout", "migration", "revamp", "redesign", "sprint", "roadmap", "initiative",
            "program", "rewrite", "audit", "upgrade", "integration", "overhaul")
_P_PROJECT = re.compile(r"\b[Pp]roject[\s:-]+([A-Z][A-Za-z0-9-]+(?:\s[A-Z][A-Za-z0-9-]+)?)")
_P_SUFFIX = re.compile(r"\b([A-Z][A-Za-z0-9-]+(?:\s[A-Z][A-Za-z0-9-]+){0,2})\s+(?i:(" + "|".join(SUFFIXES) + r"))\b")
_P_CHAN = re.compile(r"#(?:proj|project)[-_]([a-z][a-z0-9-]{2,})")
_P_PREFIX = re.compile(r"^\s*([A-Z0-9][A-Za-z0-9]*(?:[ \t]+[A-Z0-9][A-Za-z0-9]*){0,3})\s+[-–—|:]\s+")
_TOKEN = re.compile(r"[A-Za-z0-9][\w'-]*")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:100]


def _trim(tokens: list[str]) -> list[str]:
    while tokens and tokens[0].lower() in STOP:
        tokens = tokens[1:]
    while tokens and tokens[-1].lower() in STOP:
        tokens = tokens[:-1]
    return tokens


def _head(tokens: list[str]) -> str:
    h = tokens[0].lower()
    return " ".join(t.lower() for t in tokens) if h in GENERIC_HEADS and len(tokens) > 1 else h


def extract_candidates(text: str) -> list[tuple[str, str, float]]:
    """Return project name candidates; structured titles/channels are stronger than incidental capitals."""
    out: list[tuple[str, str, float]] = []
    prefix = _P_PREFIX.match(text or "")
    if prefix:
        surface = prefix.group(1).strip()
        toks = surface.split()
        if any(any(ch.isdigit() for ch in token) or (not token.isupper() and any(ch.isupper() for ch in token[1:]))
               for token in toks):
            out.append((_head(toks), surface, 1.5))
    for m in _P_PROJECT.finditer(text):
        toks = _trim(m.group(1).split())
        if toks and toks[0].lower() not in STOP:
            out.append((_head(toks), " ".join(toks), 1.0))
    for m in _P_SUFFIX.finditer(text):
        toks = _trim(m.group(1).split())
        if toks:
            suffix = m.group(2).lower()
            surface = " ".join(toks) if suffix == "project" else f"{' '.join(toks)} {suffix.title()}"
            out.append((_head(toks), surface, 1.0))
    for m in _P_CHAN.finditer(text):
        tokens = m.group(1).split("-")
        head_tokens = _trim(tokens)
        if head_tokens:
            out.append((_head(head_tokens), " ".join(tokens).title(), 1.0))
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        toks = _TOKEN.findall(sentence)
        for tok in toks[1:]:
            if len(tok) >= 4 and tok[0].isupper() and tok[1:].islower() and tok.lower() not in STOP:
                out.append((tok.lower(), tok, 0.5))
    return out


def analyze(rows: list[dict], person_tokens: set[str]) -> dict[str, dict]:
    """Pure: rows have id, text, author, day. Returns qualified heads with evidence."""
    heads: dict[str, dict] = {}
    for r in rows:
        seen_here = {}
        for head, surface, w in extract_candidates(r["text"] or ""):
            if head in STOP or head in person_tokens or len(head) < 3:
                continue
            h = heads.setdefault(head, {"strong": set(), "weak": set(), "structured": set(), "authors": set(), "days": set(), "surfaces": Counter()})
            (h["strong"] if w >= 1 else h["weak"]).add(r["id"])
            if w >= 1.5:
                h["structured"].add(r["id"])
            h["authors"].add(r["author"]); h["days"].add(r["day"])
            if w >= 1 or head not in seen_here:
                h["surfaces"][surface] += 1
            seen_here[head] = True
    need = settings.project_min_messages
    qualified = {}
    for head, h in heads.items():
        strong, weak = len(h["strong"]), len(h["weak"] - h["strong"])
        breadth = len(h["authors"]) >= 2 or len(h["days"]) >= 2
        if (strong >= need and (breadth or len(h["structured"]) >= need)) or (
                strong + weak >= max(8, need * 3) and len(h["authors"]) >= 3 and len(h["days"]) >= 2):
            surfaces = h["surfaces"].most_common()
            top = max(surfaces, key=lambda kv: (kv[1], -len(kv[0])))[0]
            qualified[head] = {"name": top, "keywords": sorted({head, *[s.lower() for s, _ in surfaces[:4]]}),
                               "count": strong + weak, "authors": len(h["authors"])}
    return qualified


def keyword_regex(keywords: list[str]) -> re.Pattern | None:
    kws = sorted({k.strip().lower() for k in keywords if k.strip()}, key=len, reverse=True)
    patterns = []
    for keyword in kws:
        parts = [part for part in re.split(r"[\s_-]+", keyword) if part]
        if parts:
            patterns.append(r"[\s_-]+".join(re.escape(part) for part in parts))
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(patterns) + r")(?![a-z0-9])", re.I) if patterns else None


_matchers: dict[tuple[int, int], tuple[float, list[tuple[int, re.Pattern]]]] = {}


def invalidate(owner_id: int) -> None:
    for key in [key for key in _matchers if key[1] == owner_id]:
        _matchers.pop(key, None)


def _get_matchers(db: Session, owner_id: int):
    key = (id(db.get_bind()), owner_id)
    hit = _matchers.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    pats = []
    for p in db.query(Project).filter_by(owner_id=owner_id, status="active").all():
        rx = keyword_regex(p.keywords.split(","))
        if rx:
            pats.append((p.id, rx))
    _matchers[key] = (time.time(), pats)
    return pats


def best_project(text: str, matchers) -> int | None:
    ranked = []
    for pid, rx in matchers:
        hits = list(rx.finditer(text or ""))
        if hits:
            lengths = [len(hit.group(0)) for hit in hits]
            ranked.append(((max(lengths), sum(lengths), len(lengths)), pid))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def matching_messages(db: Session, owner_id: int, keywords: list[str]) -> list[SlackMessage]:
    """Return non-deleted owner messages matching the same boundaries as project tagging."""
    if not keywords:
        return []
    rx = keyword_regex(keywords)
    if not rx:
        return []
    conditions = []
    for keyword in keywords:
        parts = [part for part in re.split(r"[\s_-]+", keyword.lower()) if part]
        token_conditions = []
        for part in parts:
            escaped = part.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            token_conditions.append(SlackMessage.text.ilike(f"%{escaped}%", escape="\\"))
        if token_conditions:
            conditions.append(and_(*token_conditions))
    rows = (db.query(SlackMessage)
            .filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False), or_(*conditions))
            .order_by(SlackMessage.message_created_at.desc()).all())
    return [row for row in rows if rx.search(row.text or "")]


def create_manual(db: Session, owner_id: int, name: str, keywords: list[str]) -> tuple[Project, int, int]:
    """Create/refresh a user-defined project and tag matching messages immediately."""
    slug = _slug(name)
    project = db.query(Project).filter_by(owner_id=owner_id, slug=slug).first()
    if project is None:
        project = Project(owner_id=owner_id, slug=slug, name=name[:120], keywords="")
        db.add(project)
        db.flush()
    else:
        project.status = "active"
        if not project.renamed:
            project.name = name[:120]
    project.keywords = ",".join(sorted((set(project.keywords.split(",")) | set(keywords)) - {""}))
    db.flush()
    invalidate(owner_id)
    matchers = _get_matchers(db, owner_id)
    changed = 0
    matches = matching_messages(db, owner_id, keywords)
    for row in matches:
        pid = best_project(row.text, matchers)
        if row.project_id != pid:
            row.project_id = pid
            changed += 1
    db.flush()
    refresh_stats(db, owner_id)
    log_event(db, "project", "ok", f"manual project {project.name}: {changed} messages tagged", owner_id=owner_id)
    db.commit()
    invalidate(owner_id)
    return project, changed, len(matches)


def refresh_stats(db: Session, owner_id: int) -> None:
    stats = (db.query(SlackMessage.project_id, func.count(SlackMessage.id), func.sum(case((SlackMessage.priority_label == "P0", 1), else_=0)),
                      func.min(SlackMessage.message_created_at), func.max(SlackMessage.message_created_at))
             .filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False), SlackMessage.project_id.isnot(None))
             .group_by(SlackMessage.project_id).all())
    by_id = {s[0]: s for s in stats}
    for project in db.query(Project).filter_by(owner_id=owner_id).all():
        stat = by_id.get(project.id)
        project.message_count, project.p0_count = (stat[1], int(stat[2] or 0)) if stat else (0, 0)
        if stat:
            project.first_seen_at, project.last_activity_at = stat[3], stat[4]
        else:
            project.first_seen_at, project.last_activity_at = None, None


def assign_project(db: Session, row: SlackMessage) -> int | None:
    """Live path: tag one message. Replies without a keyword inherit their parent's project."""
    pid = best_project(row.text, _get_matchers(db, row.owner_id))
    if pid is None and row.thread_ts and row.thread_ts != row.slack_ts:
        parent = db.query(SlackMessage.project_id).filter_by(connection_id=row.connection_id, channel_id=row.channel_id, slack_ts=row.thread_ts).first()
        pid = parent[0] if parent else None
    if pid != row.project_id:
        row.project_id = pid
        if pid:
            db.query(Project).filter_by(id=pid).update({Project.message_count: Project.message_count + 1, Project.last_activity_at: utcnow()})
            log_event(db, "project", "ok", f"tagged project #{pid}", owner_id=row.owner_id, message_id=row.id)
    return pid


def discover(db: Session, owner_id: int) -> dict:
    """Batch path: find projects from recurring keywords, tag messages, refresh statistics."""
    cutoff = utcnow() - timedelta(days=settings.project_lookback_days)
    cols = (SlackMessage.id, SlackMessage.text, SlackMessage.user_id, SlackMessage.message_created_at, SlackMessage.thread_ts,
            SlackMessage.slack_ts, SlackMessage.channel_id, SlackMessage.connection_id, SlackMessage.project_id)
    rows = (db.query(*cols).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False), SlackMessage.message_created_at >= cutoff)
            .order_by(SlackMessage.id.desc()).limit(30000).all())
    teams = [t for (t,) in db.query(SlackConnection.team_id).filter_by(owner_id=owner_id).all()]
    people = set()
    for (n,) in db.query(SlackUser.display_name).filter(SlackUser.team_id.in_(teams)).all() if teams else []:
        people |= {t.lower() for t in re.findall(r"[A-Za-z]+", n or "")}
    data = [{"id": r.id, "text": r.text, "author": r.user_id, "day": r.message_created_at.date() if r.message_created_at else None} for r in rows]
    qualified = analyze(data, people)

    existing = {p.slug: p for p in db.query(Project).filter_by(owner_id=owner_id).all()}
    created = 0
    for head, info in qualified.items():
        slug = _slug(head)
        proj = existing.get(slug)
        if proj is None:
            proj = Project(owner_id=owner_id, slug=slug, name=info["name"][:120], keywords=",".join(info["keywords"]))
            try:
                with db.begin_nested():
                    db.add(proj); db.flush()
                created += 1
            except IntegrityError:
                proj = db.query(Project).filter_by(owner_id=owner_id, slug=slug).one()
            existing[slug] = proj
        else:
            if not proj.renamed:
                proj.name = info["name"][:120]
            proj.keywords = ",".join(sorted(set(proj.keywords.split(",")) | set(info["keywords"]) - {""}))
    db.flush()
    invalidate(owner_id)
    matchers = _get_matchers(db, owner_id)

    assign: dict[int, list[int]] = defaultdict(list)
    clear_ids = []
    thread_project: dict[tuple, int] = {}
    pending_replies = []
    for r in rows:
        pid = best_project(r.text, matchers)
        root = r.thread_ts or r.slack_ts
        if pid:
            thread_project.setdefault((r.connection_id, r.channel_id, root), pid)
            if pid != r.project_id:
                assign[pid].append(r.id)
        elif r.thread_ts:
            pending_replies.append(r)
        elif r.project_id is not None:
            clear_ids.append(r.id)
    for r in pending_replies:  # replies inherit the thread's project
        pid = thread_project.get((r.connection_id, r.channel_id, r.thread_ts))
        if pid and pid != r.project_id:
            assign[pid].append(r.id)
        elif not pid and r.project_id is not None:
            clear_ids.append(r.id)
    changed = [i for ids in assign.values() for i in ids]
    for pid, ids in assign.items():
        for i in range(0, len(ids), 500):
            db.query(SlackMessage).filter(SlackMessage.id.in_(ids[i:i + 500])).update({"project_id": pid}, synchronize_session=False)
    for i in range(0, len(clear_ids), 500):
        db.query(SlackMessage).filter(SlackMessage.id.in_(clear_ids[i:i + 500])).update({"project_id": None}, synchronize_session=False)
    refresh_stats(db, owner_id)
    log_event(db, "project", "ok", f"discovery: {len(qualified)} qualified, {created} new, {len(changed)} messages tagged", owner_id=owner_id)
    db.commit()
    return {"projects": len(existing), "created": created, "tagged": len(changed) + len(clear_ids), "changed_ids": changed + clear_ids}
