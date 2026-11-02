"""Project discovery from keywords that keep coming up in a user's Slack messages.

No LLM is needed for this step (so it is free, fast, deterministic and testable); the LLM only writes summaries.

How a project is found
  1. Each message is tokenised. Candidate project terms are
       * capitalised words in mid-sentence ("Phoenix"), hyphen/underscore compounds ("checkout-api"),
       * two-word phrases that contain such a word or end in a project-type noun ("pricing redesign", "Q4 roadmap"),
       * explicit mentions: "project Atlas", "Atlas project", and channels named proj-*/project-*.
     People's names (from the Slack user cache), @mentions, URLs and stop-words never become candidates.
  2. Each candidate is scored on how widely it is discussed: messages, distinct days, authors and channels.
     Something said five times in one hour is chatter; something discussed across days and channels is a project.
  3. Sub-phrases are merged ("phoenix" + "phoenix migration" -> one project with both as keywords).
  4. Projects are upserted, every message is re-linked to the projects whose keywords it contains, and stats are refreshed.
Users can rename, confirm, merge, archive or create projects; confirmed/archived projects are never changed by discovery.
"""
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import integrations, pipeline_events
from app.jobs import PROJECT_DISCOVER, PROJECT_SUMMARIZE, enqueue_job
from app.config import settings
from app.models import (MessageProject, Project, ProjectKeyword, SlackConnection, SlackConversation, SlackMessage, SlackUser, User, utcnow)

log = logging.getLogger("contextos.projects")

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*")
SENT_SPLIT = re.compile(r"[.!?;:\n]+\s*")
STRIP_RE = re.compile(r"https?://\S+|`[^`]*`|@[\w.\-]+|#[\w\-]+|<[^>]+>")
PROJ_PHRASE = re.compile(r"\bproject\s+([A-Z][\w-]*(?:\s+[A-Z][\w-]*)?)")
PROJ_PHRASE_POST = re.compile(r"\b([A-Z][\w-]*(?:\s+[A-Z][\w-]*)?)\s+(?:project|initiative|workstream)\b")
CHANNEL_PREFIX = re.compile(r"^(?:proj(?:ect)?|prj|team)[-_]")
NAME_SUFFIX = {"roadmap", "launch", "migration", "redesign", "integration", "rollout", "release", "onboarding", "rebrand", "audit",
               "pilot", "initiative", "upgrade", "revamp", "overhaul", "cleanup", "refactor", "campaign", "beta", "sprint", "plan"}
ACRONYMS = {"api", "ui", "ux", "pr", "qa", "ci", "cd", "ok", "eod", "asap", "fyi", "eta", "faq", "pm", "hr", "it", "ceo", "cto", "cfo",
            "sla", "kpi", "okr", "url", "sso", "sdk", "db", "aws", "gcp", "ios", "cs", "tbd", "wfh", "ooo", "lgtm", "rfc", "pto", "cc"}
STOP = set("""
a about above after again all also am an and any are aren't as at be because been before being below between both but by can could did do does
doing don't down during each few for from further had has have having he her here hers him his how i if in into is isn't it its just let's me more
most my no nor not now of off on once only or other our ours out over own same she should so some such than that the their theirs them then there
these they this those through to too under until up us very was we were what when where which while who whom why will with would you your yours
yes yeah yep nope hey hi hello thanks thank thx please pls sorry cool great good nice sure okay ok lol haha etc also maybe really still already
ever never always sometimes got get gets going gone go goes make made makes making take took need needs needed want wants like likes know knows
think thinks see seen look looks looking come comes check checking sent send sending share shared post posted ping pinged add added adding fix fixed
done doing did ready soon later today tonight tomorrow yesterday morning afternoon evening week weekend month year quarter day days hour hours
minute minutes monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september october
november december team everyone someone anyone everybody update updates updated meeting meetings call calls sync standup lunch coffee break
issue issues thing things stuff item items doc docs document file files link links note notes email message messages channel thread folks guys
one two three first second third last next new old high low big small any some many much more less approve approval review reviewed confirm
confirmed confirmation request requested requests feedback blocker blocked deadline urgent asap fyi eod
""".split())
SENTENCE_STARTERS = {"can", "could", "would", "should", "will", "did", "does", "is", "are", "was", "were", "please", "let", "thanks", "hey", "hi"}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:120] or "project"


def norm_key(phrase: str) -> str:
    return " ".join(re.split(r"[\s\-_]+", phrase.lower().strip()))


def display_name(forms: Counter) -> str:
    form = forms.most_common(1)[0][0].replace("_", " ")
    form = re.sub(r"\s+", " ", form).strip()
    words = []
    for w in re.split(r"([\s\-])", form):
        words.append(w.upper() if w.lower() in ACRONYMS and len(w) <= 4 else (w[:1].upper() + w[1:] if w and w[0].islower() else w))
    return "".join(words)[:100]


def _strong(tok: str, idx: int) -> bool:
    """Looks like a name rather than an everyday word: capitalised mid-sentence, or a compound like checkout-api / v2."""
    if "-" in tok or "_" in tok:
        return True
    if any(ch.isdigit() for ch in tok) and any(ch.isalpha() for ch in tok) and len(tok) > 2:
        return True
    return idx > 0 and tok[0].isupper() and len(tok) > 2 and not tok.isupper()


COMPOUND_STOP = {"sign-off", "follow-up", "check-in", "stand-up", "one-on-one", "heads-up", "roll-out", "set-up", "on-call", "go-live",
                 "e-mail", "re-review", "well-being", "long-term", "short-term", "work-life", "so-called", "up-to-date", "end-to-end"}


def _is_quarter(tok: str) -> bool:
    return re.fullmatch(r"[Qq][1-4]", tok) is not None


def _junk(tok: str) -> bool:
    """p0 / p99 / v2 (severity and version tags) and everyday compounds are never project names."""
    return tok.lower() in COMPOUND_STOP or (re.fullmatch(r"[A-Za-z]\d{1,3}", tok) is not None and not _is_quarter(tok))


def _candidates_in(text: str, names: set[str]) -> dict[str, str]:
    """Return {normalised_candidate: original_form} for one message. Keys starting with '!' are explicit mentions."""
    out: dict[str, str] = {}
    cleaned = STRIP_RE.sub(" ", text or "")
    for sent in SENT_SPLIT.split(cleaned):
        toks = WORD_RE.findall(sent)
        for i, tok in enumerate(toks):
            low = tok.lower()
            usable = low not in STOP and low not in names and len(tok) >= 3 and not _junk(tok)
            if usable and low not in ACRONYMS and _strong(tok, i):
                out.setdefault(norm_key(tok), tok)
            if i + 1 >= len(toks):
                continue
            nxt = toks[i + 1]
            nl = nxt.lower()
            if low in names or nl in names or len(nxt) < 2 or _junk(nxt):
                continue
            first_ok = ((low not in STOP and low not in ACRONYMS and len(tok) >= 3) or _is_quarter(tok)) and not _junk(tok)
            if not first_ok:
                continue
            nxt_ok = nl in NAME_SUFFIX or nl in ACRONYMS or (nl not in STOP and len(nxt) >= 3)
            if not nxt_ok:
                continue
            # (A) a name-like word followed by another word, or (B) any word followed by a project-type noun
            if (_strong(tok, i) and low not in ACRONYMS) or nl in NAME_SUFFIX:
                out.setdefault(norm_key(f"{tok} {nxt}"), f"{tok} {nxt}")
    for m in list(PROJ_PHRASE.finditer(text or "")) + list(PROJ_PHRASE_POST.finditer(text or "")):
        words = [w for w in WORD_RE.findall(m.group(1)) if w.lower() not in STOP and w.lower() not in names]
        if words:
            out.setdefault("!" + norm_key(" ".join(words)), " ".join(words))
    return out


def _keyword_regex(keyword: str) -> re.Pattern:
    parts = [re.escape(p) for p in keyword.split()]
    body = r"[\s\-_]+".join(parts)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?:s|es)?(?![A-Za-z0-9])", re.I)


def discover_candidates(rows: list[dict], names: set[str], channel_hints: dict[str, str] | None = None) -> list[dict]:
    """rows: [{id, text, day, author, channel}] -> ranked, merged candidate projects (pure function, unit-tested)."""
    stats: dict[str, dict] = {}
    forms: dict[str, Counter] = defaultdict(Counter)
    explicit: set[str] = set()
    for r in rows:
        for key, form in _candidates_in(r["text"], names).items():
            is_exp = key.startswith("!")
            k = key[1:] if is_exp else key
            s = stats.setdefault(k, {"msgs": set(), "days": set(), "authors": set(), "channels": set()})
            s["msgs"].add(r["id"]); s["days"].add(r["day"]); s["authors"].add(r["author"]); s["channels"].add(r["channel"])
            forms[k][form] += 1
            if is_exp:
                explicit.add(k)
    for chan_name in (channel_hints or {}).values():
        base = CHANNEL_PREFIX.sub("", chan_name.lower())
        if base != chan_name.lower() and base:
            k = norm_key(base)
            if k in stats:
                explicit.add(k)
            forms[k][base] += 0
    cands = []
    min_msgs, min_days = settings.project_min_messages, settings.project_min_days
    for k, s in stats.items():
        df, days, authors, channels = len(s["msgs"]), len(s["days"]), len(s["authors"]), len(s["channels"])
        exp = k in explicit
        if df < (2 if exp else min_msgs) or not (exp or days >= min_days or channels >= 2):
            continue
        boost = 2.5 if exp else 1.0
        score = math.sqrt(df) * (1 + 0.15 * min(days, 14)) * (1 + 0.2 * min(authors - 1, 5)) * (1 + 0.15 * min(channels - 1, 5)) * boost
        cands.append({"key": k, "tokens": tuple(k.split()), "msgs": s["msgs"], "df": df, "days": days, "authors": authors,
                      "channels": channels, "score": score, "forms": forms[k], "explicit": exp, "aliases": set()})
    cands.sort(key=lambda c: c["score"], reverse=True)
    pool = cands[: settings.project_max * 4]
    dropped: set[str] = set()
    for u in [c for c in pool if len(c["tokens"]) == 1]:
        if u["key"] in dropped:
            continue
        bigs = [b for b in pool if len(b["tokens"]) > 1 and u["key"] in b["tokens"] and b["key"] not in dropped]
        if not bigs:
            continue
        b = max(bigs, key=lambda x: x["df"])
        ratio = b["df"] / max(1, u["df"])
        if ratio >= 0.5:        # almost every "phoenix" message is about the "phoenix migration": one project, bigger name
            b["aliases"].add(u["key"]); b["aliases"] |= u["aliases"]
            b["score"] = max(b["score"], u["score"]); dropped.add(u["key"])
        else:                   # the phrase is just one facet of a broader topic
            u["aliases"].add(b["key"]); dropped.add(b["key"])
    kept = [c for c in pool if c["key"] not in dropped]
    # sub-phrase containment between two multi-word keepers ("q4 roadmap" vs "roadmap review"): keep the higher scoring
    final: list[dict] = []
    for c in sorted(kept, key=lambda x: x["score"], reverse=True):
        # a candidate that shares a distinctive word with a stronger one ("orion skip" vs "orion onboarding") is a facet of it
        if any(set(c["tokens"]) & set(f["tokens"]) for f in final):
            continue
        # co-occurrence: if most of its messages already belong to a stronger project, it is a facet of that project
        # ("cutover plan" lives inside "phoenix"), so keep it as an alias instead of a duplicate project
        host = next((f for f in final if len(c["msgs"] & f["msgs"]) / max(1, len(c["msgs"])) >= 0.6), None)
        if host is not None:
            host["aliases"].add(c["key"]); host["aliases"] |= c["aliases"]
            continue
        final.append(c)
        if len(final) >= settings.project_max:
            break
    for c in final:
        c["name"] = display_name(c["forms"]) if c["forms"] else c["key"].title()
        c["keywords"] = [c["key"]] + sorted(c["aliases"])
    return final


# ------------------------------------------------------------------- store ---
def _names_for_owner(db: Session, owner_id: int) -> set[str]:
    teams = [t for (t,) in db.query(SlackConnection.team_id).filter_by(owner_id=owner_id).all()]
    names: set[str] = set()
    for (dn,) in db.query(SlackUser.display_name).filter(SlackUser.team_id.in_(teams)).all() if teams else []:
        for part in re.split(r"[\s._\-]+", (dn or "").lower()):
            if len(part) >= 2:
                names.add(part)
    for (author,) in db.query(SlackMessage.author_name).filter(SlackMessage.owner_id == owner_id, SlackMessage.author_name.isnot(None)).distinct().limit(500).all():
        for part in re.split(r"[\s._\-]+", (author or "").lower()):
            if len(part) >= 2:
                names.add(part)
    return names


def load_index(db: Session, owner_id: int) -> list[tuple[int, list[re.Pattern]]]:
    """Active projects with compiled keyword regexes."""
    rows = (db.query(ProjectKeyword.project_id, ProjectKeyword.keyword).join(Project, Project.id == ProjectKeyword.project_id)
            .filter(Project.owner_id == owner_id, Project.status.in_(("auto", "confirmed"))).all())
    grouped: dict[int, list[re.Pattern]] = defaultdict(list)
    for pid, kw in rows:
        grouped[pid].append(_keyword_regex(kw))
    return list(grouped.items())


def match_projects(text: str, index) -> dict[int, int]:
    hits = {}
    for pid, pats in index:
        n = sum(len(p.findall(text or "")) for p in pats)
        if n:
            hits[pid] = n
    return hits


def refresh_stats(db: Session, project_ids: list[int] | None = None, owner_id: int | None = None) -> None:
    q = db.query(Project)
    if project_ids is not None:
        q = q.filter(Project.id.in_(project_ids or [0]))
    if owner_id is not None:
        q = q.filter(Project.owner_id == owner_id)
    for p in q.all():
        n, ch, au, first, last = (db.query(func.count(SlackMessage.id), func.count(func.distinct(SlackMessage.channel_id)),
                                           func.count(func.distinct(SlackMessage.user_id)), func.min(SlackMessage.message_created_at),
                                           func.max(SlackMessage.message_created_at))
                                  .join(MessageProject, MessageProject.message_id == SlackMessage.id)
                                  .filter(MessageProject.project_id == p.id, SlackMessage.is_deleted.is_(False)).one())
        if n != p.message_count:
            p.summary_dirty = True
        p.message_count, p.channel_count, p.author_count, p.first_seen_at, p.last_seen_at = n or 0, ch or 0, au or 0, first, last


def link_rows(db: Session, rows: list[SlackMessage]) -> dict[int, list[int]]:
    """Attach freshly ingested messages to the projects whose keywords they contain. Does not commit."""
    live = [r for r in rows if r is not None and r.id is not None]
    if not live:
        return {}
    out: dict[int, list[int]] = {}
    touched: set[int] = set()
    by_owner: dict[int, list[SlackMessage]] = defaultdict(list)
    for r in live:
        by_owner[r.owner_id].append(r)
    for owner_id, group in by_owner.items():
        index = load_index(db, owner_id)
        ids = [r.id for r in group]
        db.query(MessageProject).filter(MessageProject.message_id.in_(ids)).delete(synchronize_session=False)
        if not index:
            continue
        for r in group:
            hits = {} if r.is_deleted else match_projects(r.text, index)
            out[r.id] = list(hits)
            touched |= set(hits)
            for pid, n in hits.items():
                db.add(MessageProject(message_id=r.id, project_id=pid, owner_id=owner_id, hits=n))
        db.flush()
    if touched:
        refresh_stats(db, list(touched))
        db.query(Project).filter(Project.id.in_(touched)).update({"summary_dirty": True}, synchronize_session=False)
    return out


def relink_owner(db: Session, owner_id: int, msgs=None, days: int | None = None) -> int:
    """Recompute every message->project link for one user from the current keywords, then refresh stats. Does not commit."""
    if msgs is None:
        since = utcnow() - timedelta(days=days or settings.project_window_days)
        msgs = (db.query(SlackMessage.id, SlackMessage.text).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False),
                                                                      SlackMessage.message_created_at >= since)
                .order_by(SlackMessage.id.desc()).limit(20000).all())
    index = load_index(db, owner_id)
    db.query(MessageProject).filter(MessageProject.owner_id == owner_id).delete(synchronize_session=False)
    batch = []
    for m in msgs:
        for pid, n in match_projects(m.text, index).items():
            batch.append({"message_id": m.id, "project_id": pid, "owner_id": owner_id, "hits": n})
    for i in range(0, len(batch), 2000):
        db.bulk_insert_mappings(MessageProject, batch[i:i + 2000])
    db.flush()
    refresh_stats(db, owner_id=owner_id)
    return len(batch)


def discover_projects(db: Session, owner_id: int, now: datetime | None = None) -> dict:
    """Full pass for one user: find projects, upsert them, re-link every message, refresh stats. Commits."""
    now = now or utcnow()
    since = now - timedelta(days=settings.project_window_days)
    msgs = (db.query(SlackMessage.id, SlackMessage.text, SlackMessage.message_created_at, SlackMessage.user_id, SlackMessage.channel_id,
                     SlackMessage.is_bot, SlackMessage.is_self)
            .filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False), SlackMessage.message_created_at >= since)
            .order_by(SlackMessage.id.desc()).limit(20000).all())
    user = db.get(User, owner_id)
    report = {"messages_considered": len(msgs), "projects": 0, "linked": 0, "created": 0, "archived_stale": 0}
    if user is not None:
        user.projects_discovered_at = now
    if len(msgs) < 2:
        db.commit()
        return report
    conv_names = {cid: n for cid, n in db.query(SlackConversation.channel_id, SlackConversation.name)
                  .join(SlackConnection, SlackConnection.id == SlackConversation.connection_id)
                  .filter(SlackConnection.owner_id == owner_id, SlackConversation.name.isnot(None)).all()}
    rows = [{"id": m.id, "text": m.text, "day": (m.message_created_at or now).date().isoformat(), "author": m.user_id or "?", "channel": m.channel_id}
            for m in msgs if not m.is_bot]  # bot alerts are noisy and repetitive: they are linked later but never seed projects
    found = discover_candidates(rows, _names_for_owner(db, owner_id), conv_names)

    existing = db.query(Project).filter_by(owner_id=owner_id).all()
    kw_owner: dict[str, Project] = {}
    for p in existing:
        for (kw,) in db.query(ProjectKeyword.keyword).filter_by(project_id=p.id).all():
            kw_owner[kw] = p
    matched_ids: set[int] = set()
    for c in found:
        proj = next((kw_owner[k] for k in c["keywords"] if k in kw_owner), None)
        if proj is None:
            slug, i = slugify(c["name"]), 2
            while any(e.slug == slug for e in existing):
                slug, i = f"{slugify(c['name'])}-{i}", i + 1
            proj = Project(owner_id=owner_id, name=c["name"], slug=slug, status="auto")
            db.add(proj); db.flush(); existing.append(proj)
            report["created"] += 1
        elif proj.status == "stale":
            proj.status = "auto"
        matched_ids.add(proj.id)
        if proj.status in ("archived", "confirmed"):
            continue
        proj.score = c["score"]
        have = {kw for (kw,) in db.query(ProjectKeyword.keyword).filter_by(project_id=proj.id).all()}
        for i, kw in enumerate(c["keywords"]):
            if kw not in have and kw not in kw_owner:
                db.add(ProjectKeyword(project_id=proj.id, keyword=kw, is_primary=(i == 0 and not have)))
                kw_owner[kw] = proj
    for p in existing:  # auto projects that stopped being discussed fall out of the list (not deleted: summaries stay)
        if p.status == "auto" and p.id not in matched_ids:
            p.status = "stale"
            report["archived_stale"] += 1
    db.flush()

    report["linked"] = relink_owner(db, owner_id, msgs=msgs)
    report["projects"] = db.query(func.count(Project.id)).filter(Project.owner_id == owner_id, Project.status.in_(("auto", "confirmed"))).scalar() or 0
    pipeline_events.record(db, "project_linked", "ok", owner_id=owner_id,
                           detail=f"discovery: {report['projects']} projects, {report['linked']} message links from {report['messages_considered']} messages")
    db.commit()
    return report


# ---------------------------------------------------------------- queries ---
def project_people(db: Session, owner_id: int, project_id: int, limit: int = 8) -> list[dict]:
    rows = (db.query(SlackMessage.author_name, SlackMessage.user_id, func.count(SlackMessage.id), func.max(SlackMessage.message_created_at))
            .join(MessageProject, MessageProject.message_id == SlackMessage.id)
            .filter(MessageProject.project_id == project_id, SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False),
                    SlackMessage.is_bot.is_(False))
            .group_by(SlackMessage.author_name, SlackMessage.user_id).order_by(func.count(SlackMessage.id).desc()).limit(limit).all())
    return [{"name": n or u or "unknown", "messages": c, "last_active": l.isoformat() if l else None} for n, u, c, l in rows]


def awaiting_reply(db: Session, owner_id: int, days: int = 14, limit: int = 20) -> list[SlackMessage]:
    """Messages that mention you or DM you, scored P0-P2, with no later message from you in that conversation/thread."""
    since = utcnow() - timedelta(days=days)
    cands = (db.query(SlackMessage).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False), SlackMessage.is_self.is_(False),
                                           SlackMessage.is_bot.is_(False), SlackMessage.message_created_at >= since,
                                           SlackMessage.priority_label.in_(("P0", "P1", "P2")))
             .filter((SlackMessage.mentions_me.is_(True)) | (SlackMessage.conv_type == "im"))
             .order_by(SlackMessage.priority_score.desc(), SlackMessage.message_created_at.desc()).limit(300).all())
    last_self: dict[tuple, datetime] = {}
    for conn_id, ch, mx in (db.query(SlackMessage.connection_id, SlackMessage.channel_id, func.max(SlackMessage.message_created_at))
                            .filter(SlackMessage.owner_id == owner_id, SlackMessage.is_self.is_(True), SlackMessage.is_deleted.is_(False),
                                    SlackMessage.message_created_at >= since).group_by(SlackMessage.connection_id, SlackMessage.channel_id).all()):
        last_self[(conn_id, ch)] = mx
    out = []
    for m in cands:
        mine = last_self.get((m.connection_id, m.channel_id))
        if mine is not None and m.message_created_at is not None:
            a = mine if mine.tzinfo else mine.replace(tzinfo=timezone.utc)
            b = m.message_created_at if m.message_created_at.tzinfo else m.message_created_at.replace(tzinfo=timezone.utc)
            if a >= b:
                continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------- queue ---
def enqueue_discovery(db: Session, owner_id: int, delay_seconds: int = 0):
    """At most one pending discovery per user. The job waits (reschedules) while that user's ingestion jobs are still running."""
    return enqueue_job(db, PROJECT_DISCOVER, {"owner_id": owner_id}, owner_id=owner_id, dedupe_key=f"projdisc:{owner_id}", delay_seconds=delay_seconds, max_attempts=3)


def enqueue_summary(db: Session, project_id: int, force: bool = False, owner_id: int | None = None):
    if owner_id is None:
        owner_id = db.query(Project.owner_id).filter(Project.id == project_id).scalar()
    return enqueue_job(db, PROJECT_SUMMARIZE, {"project_id": project_id, "force": force}, owner_id=owner_id,
                       dedupe_key=f"projsum:{project_id}", max_attempts=3)


def schedule_discovery(db: Session, owner_id: int, *, immediate: bool = False):
    """Debounced trigger used by ingestion: the first discovery for a user runs right away (they are waiting to see
    projects), later ones wait N minutes so a burst of messages causes ONE pass. Never raises. Caller commits."""
    try:
        first = db.query(User.projects_discovered_at).filter_by(id=owner_id).scalar() is None
        delay = 0 if (immediate or first) else settings.project_discover_minutes * 60
        return enqueue_discovery(db, owner_id, delay_seconds=delay)
    except Exception:
        log.exception("schedule_discovery_failed")
        return None


def sync_external_indexes(db: Session, owner_id: int) -> dict:
    """After discovery: mirror projects + message links into Qdrant (project filter) and Neo4j (relationship queries).
    Best effort. A failure is reported, never raised, because PostgreSQL already has the truth."""
    prs = [{"id": i, "name": n} for i, n in db.query(Project.id, Project.name).filter(
        Project.owner_id == owner_id, Project.status.in_(("auto", "confirmed"))).all()]
    links: dict[int, list[int]] = defaultdict(list)
    for mid, pid in db.query(MessageProject.message_id, MessageProject.project_id).filter(MessageProject.owner_id == owner_id).limit(100000):
        links[mid].append(pid)
    out = {}
    if prs:
        indexed = {i for (i,) in db.query(SlackMessage.id).filter(SlackMessage.owner_id == owner_id, SlackMessage.indexed_at.isnot(None)).limit(100000)}
        vec_links = {m: links.get(m, []) for m in indexed}
        out["qdrant"] = integrations.set_message_projects(owner_id, vec_links)
        out["neo4j"] = integrations.sync_project_graph(owner_id, prs, dict(links))
    inactive = [i for (i,) in db.query(Project.id).filter(Project.owner_id == owner_id, Project.status.in_(("stale", "archived"))).all()]
    integrations.delete_summary_index(inactive)
    return out


# ------------------------------------------------------------ derived signals ---
def related_projects(db: Session, owner_id: int, project_id: int, limit: int = 5) -> tuple[list[dict], str]:
    """Projects that share people with this one (hidden dependencies / overloaded people). Graph first, PostgreSQL fallback."""
    g = integrations.graph_related_projects(owner_id, project_id, limit)
    if g is not None:
        return g, "neo4j"
    names = [n for (n,) in db.query(func.distinct(func.lower(SlackMessage.author_name))).join(MessageProject, MessageProject.message_id == SlackMessage.id)
             .filter(MessageProject.project_id == project_id, SlackMessage.owner_id == owner_id, SlackMessage.is_bot.is_(False),
                     SlackMessage.is_self.is_(False), SlackMessage.author_name.isnot(None)).all()]
    if not names:
        return [], "postgresql"
    rows = (db.query(Project.id, Project.name, func.count(func.distinct(func.lower(SlackMessage.author_name))))
            .join(MessageProject, MessageProject.project_id == Project.id).join(SlackMessage, SlackMessage.id == MessageProject.message_id)
            .filter(Project.owner_id == owner_id, Project.id != project_id, Project.status.in_(("auto", "confirmed")),
                    func.lower(SlackMessage.author_name).in_(names))
            .group_by(Project.id, Project.name).order_by(func.count(func.distinct(func.lower(SlackMessage.author_name))).desc()).limit(limit).all())
    return [{"id": i, "name": n, "shared_people": c} for i, n, c in rows], "postgresql"


def key_collaborators(db: Session, owner_id: int) -> tuple[list[dict], str]:
    """People active across several projects. Neo4j when configured; otherwise the same question answered from PostgreSQL.
    Matched by display name so a person on Slack and in email counts once."""
    mp, mm = settings.collaborator_min_projects, settings.collaborator_min_messages
    g = integrations.graph_key_people(owner_id, mp, mm)
    if g is not None:
        merged: dict[str, dict] = {}
        for x in g:
            k = (x["name"] or "").strip().lower()
            if k:
                cur = merged.setdefault(k, {"name": x["name"], "projects": 0, "messages": 0})
                cur["projects"] = max(cur["projects"], x["projects"]); cur["messages"] += x["messages"]
        return [v for v in merged.values() if v["projects"] >= mp], "neo4j"
    lname = func.lower(SlackMessage.author_name)
    rows = (db.query(lname, func.max(SlackMessage.author_name), func.count(func.distinct(MessageProject.project_id)), func.count(SlackMessage.id))
            .join(MessageProject, MessageProject.message_id == SlackMessage.id)
            .join(Project, Project.id == MessageProject.project_id)
            .filter(SlackMessage.owner_id == owner_id, SlackMessage.is_bot.is_(False), SlackMessage.is_self.is_(False), SlackMessage.is_deleted.is_(False),
                    SlackMessage.author_name.isnot(None), Project.status.in_(("auto", "confirmed")))
            .group_by(lname).having(func.count(func.distinct(MessageProject.project_id)) >= mp).having(func.count(SlackMessage.id) >= mm)
            .order_by(func.count(func.distinct(MessageProject.project_id)).desc()).limit(40).all())
    return [{"name": n, "projects": p, "messages": m} for _, n, p, m in rows], "postgresql"


def apply_people_boost(db: Session, owner_id: int, days: int = 14) -> dict:
    """Raise recent P2/P3 messages written by key collaborators (never to P0, never for bots or yourself). Idempotent:
    a message already carrying the 'key collaborator' reason is skipped. Commits."""
    people, source = key_collaborators(db, owner_id)
    if not people:
        return {"boosted": 0, "source": source, "collaborators": 0}
    by_name = {(x["name"] or "").strip().lower(): x for x in people}
    since = utcnow() - timedelta(days=days)
    rows = (db.query(SlackMessage).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False), SlackMessage.is_bot.is_(False),
                                          SlackMessage.is_self.is_(False), SlackMessage.message_created_at >= since,
                                          SlackMessage.priority_label.in_(("P2", "P3")), SlackMessage.priority_score >= 25,  # some real signal; chatter stays chatter
                                          SlackMessage.author_name.isnot(None),
                                          func.lower(SlackMessage.author_name).in_(list(by_name))).limit(2000).all())
    n = 0
    for r in rows:
        if "key collaborator" in (r.reason or ""):
            continue
        info = by_name[r.author_name.strip().lower()]
        r.priority_score = min(64, r.priority_score + settings.collaborator_boost)  # capped below P1: a collaborator is a hint, not an alarm
        r.priority_label = "P2" if r.priority_score >= 35 else "P3"
        r.reason = (r.reason + "; " if r.reason else "") + f"from a key collaborator (active in {info['projects']} projects)"
        n += 1
    if n:
        pipeline_events.record(db, "graph_boost", "ok", owner_id=owner_id,
                               detail=f"{n} message(s) from {len(people)} key collaborator(s) raised ({source})")
    else:
        pipeline_events.record(db, "graph_boost", "skipped", owner_id=owner_id, detail=f"{len(people)} key collaborator(s) found ({source}); nothing new to raise")
    db.commit()
    return {"boosted": n, "source": source, "collaborators": len(people)}


def semantic_link(db: Session, owner_id: int) -> dict:
    """Link important messages that no keyword caught to the project whose summary is semantically closest (Qdrant).
    Links carry hits=0 so they are distinguishable from keyword links; relink_owner() recreates keyword links only. Commits."""
    if not settings.qdrant_enabled:
        return {"linked": 0, "skipped": "qdrant off"}
    since = utcnow() - timedelta(days=settings.project_window_days)
    linked_ids = db.query(MessageProject.message_id).filter(MessageProject.owner_id == owner_id).scalar_subquery()
    cands = (db.query(SlackMessage).filter(SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False), SlackMessage.is_bot.is_(False),
                                           SlackMessage.priority_label.in_(("P0", "P1", "P2")), SlackMessage.message_created_at >= since,
                                           ~SlackMessage.id.in_(linked_ids))
             .order_by(SlackMessage.priority_score.desc()).limit(settings.semantic_link_max_per_run).all())
    if not cands:
        return {"linked": 0}
    res = integrations.nearest_projects([m.text[:500] for m in cands], owner_id, settings.semantic_link_min_score)
    if res is None:
        return {"linked": 0, "skipped": "qdrant unavailable or no summaries indexed yet"}
    valid = {i for (i,) in db.query(Project.id).filter(Project.owner_id == owner_id, Project.status.in_(("auto", "confirmed"))).all()}
    touched, links = set(), {}
    for idx, pid, score in res:
        if pid in valid:
            db.add(MessageProject(message_id=cands[idx].id, project_id=pid, owner_id=owner_id, hits=0))
            touched.add(pid); links[cands[idx].id] = [pid]
    db.flush()
    if touched:
        refresh_stats(db, list(touched))
        db.query(Project).filter(Project.id.in_(touched)).update({"summary_dirty": True}, synchronize_session=False)
        integrations.set_message_projects(owner_id, {mid: pids for mid, pids in links.items()})
        pipeline_events.record(db, "semantic_linked", "ok", owner_id=owner_id,
                               detail=f"{len(links)} message(s) had no keyword match; linked by meaning to {len(touched)} project(s)")
    else:
        pipeline_events.record(db, "semantic_linked", "skipped", owner_id=owner_id, detail=f"{len(cands)} unlinked message(s) checked; none close enough")
    db.commit()
    return {"linked": len(links), "checked": len(cands)}
