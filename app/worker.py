"""Run with `python -m app.worker` in a separate terminal.
PostgreSQL is the durable queue of record; this worker is intentionally simple for a single-machine MVP.
For multiple replicas, row locking uses FOR UPDATE SKIP LOCKED on PostgreSQL.
"""
import json, logging, time
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from app.config import settings
from app.database import Base, engine, SessionLocal
from app.models import ProcessingJob, SlackMessage, Reminder, SuggestedEvent
from app.priority import score_message, extract_reminder_due, extract_meeting_suggestion
from app.cache import invalidate_message_caches
from app.integrations import upsert_message_vector, sync_message_graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log=logging.getLogger("contextos.worker")
Base.metadata.create_all(bind=engine)

def process_slack_event(db, payload):
    event=payload.get("event") or {}
    event_type=event.get("type") or payload.get("type")
    if event_type != "message" or event.get("subtype") or event.get("bot_id"):
        return {"ignored":True,"reason":"unsupported event type, subtype, or bot message"}
    team_id=payload.get("team_id") or (payload.get("team") or {}).get("id")
    channel_id=event.get("channel"); slack_ts=event.get("ts"); body=event.get("text") or ""
    if not team_id or not channel_id or not slack_ts:
        return {"ignored":True,"reason":"message missing team/channel/timestamp"}
    existing=db.query(SlackMessage).filter_by(team_id=team_id,channel_id=channel_id,slack_ts=str(slack_ts)).first()
    if existing: return {"duplicate_message":True,"message_id":existing.id}
    score,label,reason=score_message(body)
    try:
        created_at=datetime.fromtimestamp(float(slack_ts),tz=timezone.utc)
    except (ValueError,OverflowError,OSError):
        created_at=datetime.now(timezone.utc)
    row=SlackMessage(team_id=team_id,channel_id=channel_id,slack_ts=str(slack_ts),user_id=event.get("user"),
        text=body,priority_score=score,priority_label=label,reason=reason,source_event_id=payload.get("event_id"),message_created_at=created_at)
    db.add(row); db.flush()
    due=extract_reminder_due(body, now=created_at)
    if due:
        # Avoid multiple reminders from duplicate/replayed jobs.
        found=db.query(Reminder).filter_by(message_id=row.id).first()
        if not found: db.add(Reminder(message_id=row.id,title=body[:280] or "Follow up from Slack",due_at=due))
    meeting=extract_meeting_suggestion(body,now=created_at)
    if meeting:
        title,start=meeting
        if not db.query(SuggestedEvent).filter_by(message_id=row.id).first():
            db.add(SuggestedEvent(message_id=row.id,title=title,proposed_start=start,duration_minutes=30,source_text=body))
    db.flush()
    # External indexing is best-effort; a Qdrant/Neo4j outage must not roll back the source-of-truth message.
    integration_results={}
    try: integration_results["qdrant"]=upsert_message_vector(row)
    except Exception as exc: integration_results["qdrant"]=f"error: {type(exc).__name__}: {exc}"; log.warning("qdrant_index_failed message_id=%s error=%s",row.id,exc)
    try: integration_results["neo4j"]=sync_message_graph(row)
    except Exception as exc: integration_results["neo4j"]=f"error: {type(exc).__name__}: {exc}"; log.warning("neo4j_sync_failed message_id=%s error=%s",row.id,exc)
    invalidate_message_caches()
    return {"message_id":row.id,"priority":label,"score":score,"integrations":integration_results}

def claim_job(db):
    # PostgreSQL locks prevent two workers claiming the same job. SQLite fallback is not intended for production.
    stmt=select(ProcessingJob).where(ProcessingJob.status=="queued",ProcessingJob.available_at<=datetime.now(timezone.utc)).order_by(ProcessingJob.id).limit(1).with_for_update(skip_locked=True)
    job=db.execute(stmt).scalars().first()
    if not job: return None
    job.status="processing"; job.locked_at=datetime.now(timezone.utc); job.attempts+=1
    db.commit(); db.refresh(job)
    return job.id

def handle_one():
    with SessionLocal() as db:
        job_id=claim_job(db)
    if not job_id: return False
    with SessionLocal() as db:
        job=db.query(ProcessingJob).filter_by(id=job_id).first()
        if not job: return True
        try:
            payload=json.loads(job.payload_json)
            if job.job_type=="slack_event": result=process_slack_event(db,payload)
            else: result={"ignored":True,"reason":f"unknown job type: {job.job_type}"}
            job.status="completed"; job.finished_at=datetime.now(timezone.utc); job.last_error=None
            db.commit()
            log.info("job_completed id=%s result=%s",job_id,result)
        except Exception as exc:
            db.rollback()
            job=db.query(ProcessingJob).filter_by(id=job_id).first()
            if not job: return True
            job.last_error=str(exc)[:4000]
            if job.attempts>=job.max_attempts:
                job.status="failed"; job.finished_at=datetime.now(timezone.utc)
            else:
                job.status="queued"; job.available_at=datetime.now(timezone.utc)+timedelta(seconds=min(300,2**job.attempts))
            db.commit()
            log.exception("job_failed id=%s attempt=%s status=%s",job_id,job.attempts,job.status)
    return True

def main():
    log.info("ContextOS worker started; polling PostgreSQL queue")
    while True:
        try:
            worked=handle_one()
            if not worked: time.sleep(max(0.2,settings.worker_poll_seconds))
        except KeyboardInterrupt:
            log.info("worker stopped"); break
        except Exception:
            log.exception("worker_loop_error"); time.sleep(2)

if __name__=="__main__": main()
