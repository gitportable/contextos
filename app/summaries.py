"""Summaries, daily briefing, cited Q&A and reply drafts. The LLM is optional: every function has a deterministic fallback."""
import json
import logging
import re
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app import integrations, llm
from app.models import Project, SlackConnection, SlackConversation, SlackMessage, SuggestedEvent, Summary, User, as_utc, utcnow
from app.priority import FYI_RE, MEETING_RE, REQUEST_RE, get_tz
from app.trace import log_event

log = logging.getLogger("contextos.summaries")
TONES = {"professional", "friendly", "concise", "assertive", "empathetic"}


def esc_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def open_url(row: SlackMessage, is_demo: bool = False) -> str | None:
    """Mode B: hand the user to Slack itself (no send permission needed)."""
    return None if is_demo else f"slack://channel?team={row.team_id}&id={row.channel_id}&message={row.slack_ts}"


def line(m: SlackMessage) -> str:
    return f"[#{m.id}] {m.author_name or m.user_id or 'unknown'} ({m.priority_label}): {m.text[:320]}"


def _log_llm(db, owner_id, status, detail):
    log_event(db, "llm", status, detail, owner_id=owner_id)


# --------------------------------------------------------------- projects ---
def summarize_project(db: Session, project: Project) -> Summary | None:
    msgs = (db.query(SlackMessage).filter(SlackMessage.owner_id == project.owner_id, SlackMessage.project_id == project.id,
                                          SlackMessage.is_deleted.is_(False))
            .order_by(desc(SlackMessage.priority_score), desc(SlackMessage.message_created_at)).limit(40).all())
    if not msgs:
        return None
    ids = [m.id for m in msgs]
    text, provider = None, "extractive"
    if llm.available():
        try:
            res = llm.complete(
                "You write a status summary of ONE work project from Slack messages. Use exactly these headings: Status, Decisions, "
                "Open items (with owner if stated), Risks. Short bullets, at most 170 words total. After each factual bullet cite the "
                "supporting message id like [#12]. Omit a heading if the data has nothing for it.",
                f"Project: {project.name}\n" + llm.wrap([line(m) for m in msgs]), max_tokens=420)
            text, provider = res.text, f"{res.provider}/{res.model}"
            _log_llm(db, project.owner_id, "ok", f"summary for '{project.name}' via {provider}")
        except llm.LLMUnavailable as exc:
            _log_llm(db, project.owner_id, "skipped", f"fallback to extractive: {str(exc)[:200]}")
    if text is None:
        top = msgs[:5]
        text = (f"Status: {project.message_count} messages, {project.p0_count} critical; last activity "
                f"{as_utc(project.last_activity_at).strftime('%d %b') if project.last_activity_at else 'n/a'}.\nTop items:\n"
                + "\n".join(f"- {m.text[:160]} [#{m.id}] ({m.priority_label})" for m in top))
    row = Summary(owner_id=project.owner_id, kind="project", project_id=project.id, text=text, provider=provider,
                  source_message_ids=",".join(map(str, ids)))
    db.add(row)
    project.summary_text, project.summary_at, project.summary_count, project.summary_source = text, utcnow(), project.message_count, provider
    log_event(db, "summary", "ok", f"'{project.name}' summarised from {len(ids)} messages ({provider})", owner_id=project.owner_id)
    db.commit()
    return row


# --------------------------------------------------------------- briefing ---
def daily_briefing(db: Session, user: User, today: dict, refresh: bool = False) -> dict:
    if not refresh:
        cached = (db.query(Summary).filter_by(owner_id=user.id, kind="briefing").order_by(desc(Summary.created_at)).first())
        if cached and utcnow() - as_utc(cached.created_at) < timedelta(hours=1):
            return {"text": cached.text, "provider": cached.provider, "generated_at": cached.created_at.isoformat(), "cached": True}
    counts = {k: len(v) for k, v in today.items() if isinstance(v, list)}
    items = [f"{k}: {it['text'][:140]}" for k in ("needs_now", "due_today", "to_schedule", "waiting_on_others") for it in today.get(k, [])[:4]]
    text, provider = None, "extractive"
    if items and llm.available():
        try:
            res = llm.complete("Write a calm 3-4 sentence morning briefing for a busy person. Lead with what needs them now, then what is "
                               "due today, then meetings to schedule and who they are waiting on. No greeting, no invented items.",
                               llm.wrap(items) + f"\nCounts: {json.dumps(counts)}", max_tokens=220)
            text, provider = res.text, f"{res.provider}/{res.model}"
        except llm.LLMUnavailable as exc:
            _log_llm(db, user.id, "skipped", f"briefing fallback: {str(exc)[:200]}")
    if text is None:
        text = (f"{counts.get('needs_now', 0)} item(s) need you now, {counts.get('due_today', 0)} due today, "
                f"{counts.get('to_schedule', 0)} meeting(s) to schedule, {counts.get('waiting_on_others', 0)} waiting on others.")
    row = Summary(owner_id=user.id, kind="briefing", text=text, provider=provider)
    db.add(row); db.commit()
    return {"text": text, "provider": provider, "generated_at": row.created_at.isoformat(), "cached": False}


# -------------------------------------------------------------------- ask ---
def answer_question(db: Session, user: User, question: str) -> dict:
    projects = db.query(Project).filter_by(owner_id=user.id, status="active").all()
    ql = question.lower()
    hit_projects = [p for p in projects if any(re.search(rf"\b{re.escape(k)}\b", ql) for k in p.keywords.split(",") if k)]
    ids: list[int] = []
    attention_intent = bool(re.search(r"\b(attention|need|urgent|priority|due|today|meeting|schedule|calendar)\b", ql))
    if attention_intent:
        now = utcnow()
        local_now = now.astimezone(get_tz())
        start_today = datetime.combine(local_now.date(), time.min, tzinfo=get_tz()).astimezone(timezone.utc)
        end_today = datetime.combine(local_now.date() + timedelta(days=1), time.min, tzinfo=get_tz()).astimezone(timezone.utc)
        event_ids = [message_id for (message_id,) in db.query(SuggestedEvent.message_id).filter(
            SuggestedEvent.owner_id == user.id, SuggestedEvent.status == "suggested",
            SuggestedEvent.proposed_start >= start_today - timedelta(days=1),
            SuggestedEvent.proposed_start < end_today + timedelta(days=7),
            SuggestedEvent.message_id.isnot(None)).all()]
        if event_ids:
            ids.extend(event_ids)
        attention_rows = (db.query(SlackMessage)
                          .filter(SlackMessage.owner_id == user.id, SlackMessage.is_deleted.is_(False),
                                  SlackMessage.message_created_at >= now - timedelta(days=7))
                          .order_by(desc(SlackMessage.priority_score), desc(SlackMessage.message_created_at))
                          .limit(200).all())
        ids.extend(row.id for row in attention_rows
                   if row.id in event_ids or MEETING_RE.search(row.text or "") or row.priority_score >= 65)
    for p in hit_projects[:2]:
        for h in integrations.search_vectors(question, user.id, 6, project_id=p.id):
            ids.append(h["message_id"])
    terms = [t.strip(".,?!:;()\"'") for t in ql.split() if len(t) > 3]
    q = db.query(SlackMessage).filter(SlackMessage.owner_id == user.id, SlackMessage.is_deleted.is_(False))
    if hit_projects:
        q = q.filter(SlackMessage.project_id.in_([p.id for p in hit_projects]))
    candidates = q.order_by(desc(SlackMessage.priority_score), desc(SlackMessage.message_created_at)).limit(400).all()
    keyword_matches = [(sum(term in row.text.lower() for term in terms), row.priority_score,
                        as_utc(row.message_created_at) if row.message_created_at else datetime.min.replace(tzinfo=timezone.utc), row.id)
                       for row in candidates if any(term in row.text.lower() for term in terms)]
    ids.extend(row_id for _, _, _, row_id in sorted(keyword_matches, reverse=True))
    for h in integrations.search_vectors(question, user.id, 8):
        ids.append(h["message_id"])
    seen, order = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i); order.append(i)
    # defence in depth: whatever the vector store returned, only this user's live messages are used
    rows = {r.id: r for r in db.query(SlackMessage).filter(SlackMessage.id.in_(order[:12]), SlackMessage.owner_id == user.id,
                                                           SlackMessage.is_deleted.is_(False)).all()}
    srcs = [rows[i] for i in order[:12] if i in rows][:8]
    demo = {c.id for c in db.query(SlackConnection).filter_by(owner_id=user.id, is_demo=True).all()}
    convs = {(c.connection_id, c.channel_id): c.name for c in db.query(SlackConversation).filter(SlackConversation.connection_id.in_({r.connection_id for r in srcs})).all()} if srcs else {}
    sources = [{"message_id": r.id, "text": r.text[:300], "channel": convs.get((r.connection_id, r.channel_id)) or r.channel_id,
                "author": r.author_name or r.user_id, "priority": r.priority_label, "when": r.message_created_at.isoformat() if r.message_created_at else None,
                "open_url": open_url(r, r.connection_id in demo)} for r in srcs]
    if not srcs and not hit_projects:
        return {"answer": "I couldn't find anything about that in your Slack messages yet.", "sources": [], "mode": "none", "projects": []}
    context = [f"Project summary '{p.name}': {p.summary_text}" for p in hit_projects[:2] if p.summary_text] + [line(r) for r in srcs]
    answer, mode = None, "extractive"
    if llm.available():
        try:
            res = llm.complete("Answer the user's question using ONLY the data. Be brief (max 140 words). Cite supporting messages as [#id]. "
                               "If the data is insufficient, say so plainly.", f"Question: {question}\n" + llm.wrap(context), quality=True, max_tokens=380)
            answer, mode = res.text, f"llm:{res.provider}"
            _log_llm(db, user.id, "ok", f"answer via {res.provider}/{res.model}")
        except llm.LLMUnavailable as exc:
            _log_llm(db, user.id, "skipped", f"ask fallback: {str(exc)[:200]}")
    if answer is None:
        head = "".join(f"{p.name}: {p.summary_text}\n\n" for p in hit_projects[:1] if p.summary_text)
        answer = head + "Most relevant messages:\n" + "\n".join(f"- {s['text'][:140]} [#{s['message_id']}]" for s in sources[:5])
    db.commit()
    return {"answer": answer, "sources": sources, "mode": mode, "projects": [p.name for p in hit_projects]}


# ----------------------------------------------------------- reply drafts ---
def response_type(m: SlackMessage) -> str:
    if m.is_bot:
        return "no_reply_needed"
    if FYI_RE.search(m.text):
        return "no_reply_needed"
    if MEETING_RE.search(m.text):
        return "propose_time"
    if REQUEST_RE.search(m.text) or "?" in m.text:
        return "reply_now"
    return "acknowledge"


_TEMPLATES = {
    "reply_now": {"professional": "Thanks for flagging this. I'll look into it and get back to you shortly.", "friendly": "Thanks! I'll take a look and get back to you soon.",
                  "concise": "On it, will update soon.", "assertive": "I'll handle this and confirm once it's done.", "empathetic": "Thanks for raising this, I know it matters. I'll prioritise it and update you shortly."},
    "propose_time": {"professional": "Happy to meet. Could you share a couple of times that work for you?", "friendly": "Sounds good! What times work for you?",
                     "concise": "Works. Send times?", "assertive": "Let's meet. I can do tomorrow afternoon; please confirm.", "empathetic": "Happy to find time. What suits you best?"},
    "acknowledge": {t: "Thanks, noted." for t in TONES},
    "no_reply_needed": {t: "(No reply needed.)" for t in TONES},
}


def suggest_reply(db: Session, user: User, row: SlackMessage, tone: str) -> dict:
    tone = tone if tone in TONES else "professional"
    rtype = response_type(row)
    draft, source = _TEMPLATES[rtype][tone], "template"
    if rtype != "no_reply_needed" and llm.available():
        ctx = (db.query(SlackMessage).filter(SlackMessage.owner_id == user.id, SlackMessage.connection_id == row.connection_id,
                                             SlackMessage.channel_id == row.channel_id, SlackMessage.is_deleted.is_(False),
                                             SlackMessage.id != row.id).order_by(desc(SlackMessage.message_created_at)).limit(5).all())
        proj = db.query(Project).filter_by(id=row.project_id, owner_id=user.id).first() if row.project_id else None
        try:
            res = llm.complete(f"Draft a Slack reply the user could send. Tone: {tone}. Max 70 words. Output ONLY the reply text, no quotes. "
                               "Do not promise anything specific the data does not support.",
                               (f"Project context: {proj.summary_text[:600]}\n" if proj and proj.summary_text else "") +
                               "Recent conversation:\n" + llm.wrap([line(m) for m in reversed(ctx)]) + f"\nMessage to answer:\n{llm.wrap([line(row)])}", max_tokens=180)
            draft, source = res.text.strip().strip('"'), f"{res.provider}/{res.model}"
        except llm.LLMUnavailable:
            pass
    return {"response_type": rtype, "tone": tone, "draft": draft, "source": source}
