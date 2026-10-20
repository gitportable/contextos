import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pathlib import Path
from sqlalchemy import desc, text, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.cache import get_json, set_json
from app.database import Base, engine, get_db
from app.models import EventReceipt, SlackMessage, Reminder, SuggestedEvent, AssistantNote, ProcessingJob, OutboundAction
from app.priority import score_message, extract_reminder_due, extract_meeting_suggestion
from app.slack_signing import InvalidSlackSignature, verify_slack_request
from app.schemas import ReminderCreate, EventCreate, AssistantQuery, SlackReply
from app.integrations import integration_status, search_vectors, retrieve_with_langgraph, create_calendar_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("contextos")
Base.metadata.create_all(bind=engine)
app = FastAPI(title="ContextOS Pipeline API", version="0.3.0",
 description="Slack ingestion, durable processing queue, priority, reminders, schedule suggestions, source-grounded retrieval and approved Slack replies.")

@app.get("/")
def root():
    ui = Path(__file__).parent / "static" / "index.html"
    return FileResponse(ui) if ui.exists() else {"app":"ContextOS", "status":"running", "docs":"/docs", "health":"/health"}

@app.get("/api/integrations")
def integrations():
    return integration_status()

@app.get("/api/pipeline/metrics")
def pipeline_metrics(db: Session = Depends(get_db)):
    jobs = {status: count for status, count in db.query(ProcessingJob.status, func.count(ProcessingJob.id)).group_by(ProcessingJob.status).all()}
    priorities = {p: db.query(SlackMessage).filter_by(priority_label=p).count() for p in ("P0", "P1", "P2", "P3")}
    return {"messages": db.query(SlackMessage).count(), "events_received": db.query(EventReceipt).count(), "jobs": jobs, "priorities": priorities, "reminders_pending": db.query(Reminder).filter_by(status="pending").count(), "suggestions_pending": db.query(SuggestedEvent).filter_by(status="suggested").count(), "actions_sent": db.query(OutboundAction).filter_by(status="sent").count(), "actions_failed": db.query(OutboundAction).filter_by(status="failed").count()}

@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    components = {"api":"ok", **integration_status()}
    if settings.redis_url:
        try:
            import redis
            redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1).ping()
            components["redis"] = "ok"
        except Exception as exc:
            components["redis"] = f"error: {type(exc).__name__}"
    return {"status":"ok", "components":components}

@app.post("/integrations/slack/events")
async def slack_events(
    request: Request,
    db: Session = Depends(get_db),
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
):
    raw_body = await request.body()

    try:
        verify_slack_request(
            raw_body,
            x_slack_request_timestamp,
            x_slack_signature,
        )
    except InvalidSlackSignature as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        payload: dict[str, Any] = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    event_id = payload.get("event_id")
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event_id")
    event = payload.get("event") or {}
    team_id = payload.get("team_id") or (payload.get("team") or {}).get("id")
    event_type = str(event.get("type") or payload.get("type") or "unknown")
    try:
        db.add(EventReceipt(event_id=event_id, team_id=team_id, event_type=event_type))
        # Store the full event durably before returning 200 to Slack. Worker processes it asynchronously.
        db.add(ProcessingJob(job_type="slack_event", payload_json=json.dumps(payload), status="queued"))
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"ok": True, "duplicate": True}
    logger.info("Slack event durably queued event_id=%s type=%s", event_id, event_type)
    return {"ok": True, "queued": True, "event_id": event_id}

def message_dict(row):
    return {"id":row.id,"team_id":row.team_id,"channel_id":row.channel_id,"slack_ts":row.slack_ts,
            "user_id":row.user_id,"text":row.text,"priority_score":row.priority_score,
            "priority_label":row.priority_label,"reason":row.reason,
            "message_created_at":row.message_created_at.isoformat() if row.message_created_at else None,
            "ingested_at":row.ingested_at.isoformat() if row.ingested_at else None}

@app.get("/messages")
def list_messages(limit: int = 50, priority: str | None = None, db: Session = Depends(get_db)):
    limit = max(1,min(limit,200))
    cache_key=f"contextos:messages:{limit}:{(priority or 'all').upper()}"
    cached=get_json(cache_key)
    if cached is not None: return cached
    query = db.query(SlackMessage)
    if priority:
        normalized = priority.upper()
        if normalized not in {"P0","P1","P2","P3"}:
            raise HTTPException(status_code=400, detail="priority must be P0, P1, P2, or P3")
        query = query.filter(SlackMessage.priority_label == normalized)
    rows = query.order_by(desc(SlackMessage.priority_score), desc(SlackMessage.ingested_at)).limit(limit).all()
    result=[message_dict(row) for row in rows]
    set_json(cache_key,result,ttl=15)
    return result

@app.get("/messages/{message_id}")
def get_message(message_id: int, db: Session = Depends(get_db)):
    row = db.query(SlackMessage).filter_by(id=message_id).first()
    if not row: raise HTTPException(status_code=404, detail="Message not found")
    return message_dict(row)

@app.post("/messages/{message_id}/reply")
async def reply_to_slack(message_id: int, data: SlackReply, db: Session = Depends(get_db)):
    """Send only with explicit approved=true. Requires chat:write and a configured bot token."""
    if not data.approved:
        raise HTTPException(status_code=409, detail="Reply not sent. Explicit approval is required: set approved=true after reviewing the draft.")
    if not settings.slack_bot_token:
        raise HTTPException(status_code=503, detail="SLACK_BOT_TOKEN is not configured")
    row = db.query(SlackMessage).filter_by(id=message_id).first()
    if not row: raise HTTPException(status_code=404, detail="Message not found")
    request_body = {"channel":row.channel_id, "text":data.text}
    thread_ts = data.thread_ts or row.slack_ts
    if thread_ts: request_body["thread_ts"] = thread_ts
    audit = OutboundAction(action_type="slack_reply", team_id=row.team_id, channel_id=row.channel_id,
                           message_id=row.id, request_json=json.dumps(request_body), status="sending")
    db.add(audit); db.commit(); db.refresh(audit)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post("https://slack.com/api/chat.postMessage", json=request_body,
                headers={"Authorization":f"Bearer {settings.slack_bot_token}"})
        payload = response.json()
        if response.status_code >= 400 or not payload.get("ok"):
            audit.status = "failed"; audit.error = str(payload.get("error", response.status_code))
            db.commit()
            raise HTTPException(status_code=502, detail={"message":"Slack rejected the reply", "error":audit.error, "action_id":audit.id})
        audit.status = "sent"; audit.external_id = str(payload.get("ts", "")); db.commit()
        return {"ok":True,"action_id":audit.id,"status":"sent","channel":row.channel_id,"thread_ts":payload.get("ts")}
    except HTTPException:
        raise
    except Exception as exc:
        audit.status = "failed"; audit.error = str(exc)[:1000]; db.commit()
        raise HTTPException(status_code=502, detail="Slack reply failed; see outbound action audit") from exc

@app.get("/actions")
def list_actions(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(OutboundAction).order_by(desc(OutboundAction.created_at)).limit(max(1,min(limit,200))).all()
    return [{"id":r.id,"type":r.action_type,"team_id":r.team_id,"channel_id":r.channel_id,"message_id":r.message_id,"status":r.status,"external_id":r.external_id,"error":r.error,"created_at":r.created_at.isoformat()} for r in rows]

@app.get("/queue/summary")
def queue_summary(db: Session = Depends(get_db)):
    rows = db.query(ProcessingJob.status, func.count(ProcessingJob.id)).group_by(ProcessingJob.status).all()
    return {"jobs_by_status":{status:count for status,count in rows},"worker":"run `python -m app.worker` in a second terminal"}

@app.get("/queue/jobs")
def queue_jobs(status: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    q = db.query(ProcessingJob)
    if status: q = q.filter_by(status=status)
    rows = q.order_by(desc(ProcessingJob.created_at)).limit(max(1,min(limit,200))).all()
    return [{"id":j.id,"job_type":j.job_type,"status":j.status,"attempts":j.attempts,"max_attempts":j.max_attempts,"available_at":j.available_at.isoformat(),"last_error":j.last_error,"created_at":j.created_at.isoformat()} for j in rows]

@app.post("/reminders")
def create_reminder(data: ReminderCreate, db: Session = Depends(get_db)):
    if data.message_id and not db.query(SlackMessage).filter_by(id=data.message_id).first(): raise HTTPException(status_code=404, detail="Message not found")
    due = data.due_at if data.due_at.tzinfo else data.due_at.replace(tzinfo=timezone.utc)
    row = Reminder(title=data.title, due_at=due, message_id=data.message_id); db.add(row); db.commit(); db.refresh(row)
    return {"id":row.id,"title":row.title,"due_at":row.due_at.isoformat(),"status":row.status}

@app.get("/reminders")
def list_reminders(status: str = "pending", limit: int = 100, db: Session = Depends(get_db)):
    q = db.query(Reminder)
    if status != "all": q = q.filter(Reminder.status == status)
    rows = q.order_by(Reminder.due_at).limit(max(1,min(limit,200))).all()
    return [{"id":r.id,"message_id":r.message_id,"title":r.title,"due_at":r.due_at.isoformat(),"status":r.status} for r in rows]

@app.post("/reminders/{reminder_id}/complete")
def complete_reminder(reminder_id: int, db: Session = Depends(get_db)):
    row = db.query(Reminder).filter_by(id=reminder_id).first()
    if not row: raise HTTPException(status_code=404, detail="Reminder not found")
    row.status="completed"; row.completed_at=datetime.now(timezone.utc); db.commit()
    return {"ok":True,"id":row.id,"status":row.status}

@app.get("/schedule/suggestions")
def list_suggestions(status: str = "suggested", db: Session = Depends(get_db)):
    q=db.query(SuggestedEvent)
    if status != "all": q=q.filter_by(status=status)
    return [{"id":e.id,"message_id":e.message_id,"title":e.title,"proposed_start":e.proposed_start.isoformat(),"duration_minutes":e.duration_minutes,"status":e.status,"source_text":e.source_text} for e in q.order_by(SuggestedEvent.proposed_start).limit(200).all()]

@app.post("/schedule/suggestions/{event_id}/approve")
def approve_suggestion(event_id: int, db: Session = Depends(get_db)):
    row=db.query(SuggestedEvent).filter_by(id=event_id).first()
    if not row: raise HTTPException(status_code=404, detail="Suggestion not found")
    row.status="approved"; db.commit()
    return {"ok":True,"id":row.id,"status":"approved","note":"Approval recorded. Calendar event creation requires Google Calendar OAuth and is not active until configured."}

@app.post("/assistant/ask")
def assistant_ask(data: AssistantQuery, db: Session = Depends(get_db)):
    terms=[t.lower().strip(".,?!:;()") for t in data.question.split() if len(t)>3]
    rows=db.query(SlackMessage).order_by(desc(SlackMessage.priority_score),desc(SlackMessage.ingested_at)).limit(1000).all()
    ranked=[]
    for row in rows:
        overlap=sum(1 for term in terms if term in row.text.lower())
        if overlap: ranked.append((overlap,row))
    ranked.sort(key=lambda x:(x[0],x[1].priority_score),reverse=True)
    fallback=[{"message_id":r.id,"channel_id":r.channel_id,"priority":r.priority_label,"text":r.text} for _,r in ranked[:5]]
    result=retrieve_with_langgraph(data.question, fallback)
    db.add(AssistantNote(question=data.question,answer=result["answer"],source_message_ids=",".join(str(s.get("message_id")) for s in result["sources"]))); db.commit()
    result["vector_search"]="configured" if settings.qdrant_url else "not_configured"
    return result

@app.post("/integrations/qdrant/reindex")
def qdrant_reindex(limit: int = 500, db: Session = Depends(get_db)):
    if not settings.qdrant_url: raise HTTPException(status_code=503, detail="Configure QDRANT_URL first")
    from app.integrations import upsert_message_vector
    rows=db.query(SlackMessage).order_by(SlackMessage.id.desc()).limit(max(1,min(limit,5000))).all()
    ok=0; errors=[]
    for row in rows:
        try: upsert_message_vector(row); ok+=1
        except Exception as exc: errors.append({"message_id":row.id,"error":str(exc)[:300]})
    return {"indexed":ok,"errors":errors[:20],"collection":settings.qdrant_collection}

@app.post("/schedule/suggestions/{event_id}/approve-and-create")
def approve_and_create_calendar_event(event_id: int, db: Session = Depends(get_db)):
    row=db.query(SuggestedEvent).filter_by(id=event_id).first()
    if not row: raise HTTPException(status_code=404, detail="Suggestion not found")
    try:
        created=create_calendar_event(row.title,row.proposed_start,row.duration_minutes, f"Approved ContextOS suggestion from Slack message #{row.message_id}. Source: {row.source_text}")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Calendar event not created: {exc}") from exc
    row.status="created"; db.commit()
    return {"ok":True,"id":row.id,"status":"created","calendar":created}

@app.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)):
    cached=get_json("contextos:summary")
    if cached is not None: return cached
    counts={p:db.query(SlackMessage).filter_by(priority_label=p).count() for p in ("P0","P1","P2","P3")}
    result={"messages_total":db.query(SlackMessage).count(),"by_priority":counts,"pending_reminders":db.query(Reminder).filter_by(status="pending").count(),"suggested_events":db.query(SuggestedEvent).filter_by(status="suggested").count(),"queued_jobs":db.query(ProcessingJob).filter_by(status="queued").count(),"failed_jobs":db.query(ProcessingJob).filter_by(status="failed").count()}
    set_json("contextos:summary",result,ttl=10)
    return result



@app.post("/integrations/slack/sync-history")
async def sync_slack_history(
    request: Request,
    x_contextos_admin_key: str | None = Header(default=None),
    channel_ids: str | None = None,
    limit_per_channel: int = 100,
    db: Session = Depends(get_db),
):
    """Backfill recent channel messages via Slack Web API. Requires an admin key and a bot token.
    channel_ids is an optional comma-separated list of channel IDs; otherwise public/private channels
    visible to the bot are enumerated. This queues messages for the same worker pipeline as live events.
    """
    if not settings.contextos_admin_key:
        raise HTTPException(status_code=503, detail="Set CONTEXTOS_ADMIN_KEY in .env before using history sync")
    if not x_contextos_admin_key or not __import__("hmac").compare_digest(x_contextos_admin_key, settings.contextos_admin_key):
        raise HTTPException(status_code=401, detail="Invalid x-contextos-admin-key")
    if not settings.slack_bot_token:
        raise HTTPException(status_code=503, detail="SLACK_BOT_TOKEN is not configured")
    limit_per_channel = max(1, min(limit_per_channel, 500))
    headers = {"Authorization": f"Bearer {settings.slack_bot_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        auth_resp = await client.get("https://slack.com/api/auth.test", headers=headers)
        auth_data = auth_resp.json()
        if auth_resp.status_code >= 400 or not auth_data.get("ok"):
            raise HTTPException(status_code=502, detail=f"Slack auth.test failed: {auth_data.get('error', 'unknown')}")
        workspace_id = auth_data.get("team_id") or "history-sync"
        if channel_ids:
            channels = [{"id": cid.strip()} for cid in channel_ids.split(",") if cid.strip()]
        else:
            channels = []
            cursor = None
            for _ in range(10):
                params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200}
                if cursor: params["cursor"] = cursor
                resp = await client.get("https://slack.com/api/conversations.list", headers=headers, params=params)
                if resp.status_code >= 400:
                    raise HTTPException(status_code=502, detail="Slack conversations.list request failed")
                data = resp.json()
                if not data.get("ok"):
                    raise HTTPException(status_code=502, detail=f"Slack API error: {data.get('error', 'unknown')}")
                channels.extend(data.get("channels", []))
                cursor = (data.get("response_metadata") or {}).get("next_cursor")
                if not cursor: break
        queued = 0
        skipped = 0
        channel_results = []
        for channel in channels:
            cid = channel["id"]
            resp = await client.get("https://slack.com/api/conversations.history", headers=headers,
                                    params={"channel": cid, "limit": limit_per_channel})
            if resp.status_code >= 400:
                channel_results.append({"channel_id": cid, "error": f"HTTP {resp.status_code}"})
                continue
            data = resp.json()
            if not data.get("ok"):
                channel_results.append({"channel_id": cid, "error": data.get("error", "unknown")})
                continue
            added = 0
            for message in data.get("messages", []):
                if message.get("subtype") or message.get("bot_id") or not message.get("ts"):
                    skipped += 1
                    continue
                # The worker accepts the same event envelope as live Events API messages.
                synthetic_id = f"history:{cid}:{message['ts']}"
                if db.query(ProcessingJob).filter(ProcessingJob.job_type == "slack_event",
                    ProcessingJob.payload_json.like(f'%"event_id": "{synthetic_id}"%')).first():
                    continue
                envelope = {"type": "event_callback", "event_id": synthetic_id,
                    "team_id": workspace_id,
                    "event": {"type": "message", "channel": cid, "ts": message["ts"],
                              "user": message.get("user"), "text": message.get("text", "")}}
                # Slack history doesn't return team_id; auth.test above supplies the real workspace ID.
                db.add(ProcessingJob(job_type="slack_event", payload_json=json.dumps(envelope),
                                     status="queued", available_at=datetime.now(timezone.utc)))
                queued += 1
                added += 1
            channel_results.append({"channel_id": cid, "queued": added, "fetched": len(data.get("messages", []))})
        db.commit()
    return {"ok": True, "channels_checked": len(channels), "jobs_queued": queued,
            "messages_skipped": skipped, "results": channel_results}

@app.post("/schedule/suggestions")
def create_schedule_suggestion(data: EventCreate, db: Session = Depends(get_db)):
    if data.message_id and not db.query(SlackMessage).filter_by(id=data.message_id).first():
        raise HTTPException(status_code=404, detail="Message not found")
    start = data.proposed_start if data.proposed_start.tzinfo else data.proposed_start.replace(tzinfo=timezone.utc)
    row = SuggestedEvent(message_id=data.message_id, title=data.title, proposed_start=start,
                         duration_minutes=data.duration_minutes, source_text=data.source_text)
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "title": row.title, "proposed_start": row.proposed_start.isoformat(),
            "duration_minutes": row.duration_minutes, "status": row.status}


@app.get("/dev/seed")
def seed_disabled():
    if settings.app_env != "development": raise HTTPException(status_code=403, detail="Seed endpoint disabled outside development")
    raise HTTPException(status_code=410, detail="Synthetic seed endpoint removed from default API to avoid confusing demo data with real Slack ingestion. Use tests for fixtures.")
