"""Run with `python -m app.worker` in a separate terminal.

PostgreSQL is the durable queue of record. Claiming uses FOR UPDATE SKIP LOCKED so multiple workers are safe.
Job types: slack_event, slack_process_message, slack_initial_sync, slack_incremental_sync, slack_reconcile_conversation.
Resilience: exponential backoff, rate-limit reschedule (no attempt consumed), permanent-failure short-circuit,
recovery of jobs stranded in `processing` by a crash, and a scheduler that keeps every connection syncing.
"""
import json
import logging
import time
from datetime import timedelta

from sqlalchemy import func, select

from app.cache import invalidate_user_caches
from app.config import settings
from app.database import SessionLocal, init_db
from app.jobs import (DISCOVER_PROJECTS, RETENTION, SUMMARIZE_PROJECT, PermanentJobError, RescheduleJob, SLACK_EVENT, SLACK_INCREMENTAL_SYNC, SLACK_INITIAL_SYNC,
                      SLACK_PROCESS_MESSAGE, SLACK_RECONCILE)
from app.models import (OAuthState, OutboundAction, PipelineEvent, ProcessingJob, Project, Reminder, SlackConnection, SlackConversation,
                        SlackMessage, SuggestedEvent, UserSession, WorkerHeartbeat, as_utc, utcnow)
from app import integrations, projects, summaries
from app.jobs import enqueue_job
from app.pipeline import index_rows
from app.slack_events import process_message_job, process_slack_event
from app.trace import log_event
from app.slack_sync import enqueue_sync, sync_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("contextos.worker")

SYNC_MODES = {SLACK_INITIAL_SYNC: "initial", SLACK_INCREMENTAL_SYNC: "incremental", SLACK_RECONCILE: "reconcile"}


def claim_job(db):
    # PostgreSQL row locks stop two workers claiming the same job. (SQLite ignores SKIP LOCKED: tests/dev only.)
    stmt = (select(ProcessingJob).where(ProcessingJob.status == "queued", ProcessingJob.available_at <= utcnow())
            .order_by(ProcessingJob.id).limit(1).with_for_update(skip_locked=True))
    job = db.execute(stmt).scalars().first()
    if not job:
        return None
    job.status, job.locked_at, job.attempts = "processing", utcnow(), job.attempts + 1
    db.commit()
    return job.id


def dispatch(db, job, payload):
    jt = job.job_type
    if jt == SLACK_EVENT:
        return process_slack_event(db, payload)
    if jt == SLACK_PROCESS_MESSAGE:
        return process_message_job(db, payload)
    if jt == DISCOVER_PROJECTS:
        return run_discovery(db, payload["owner_id"])
    if jt == SUMMARIZE_PROJECT:
        proj = db.get(Project, payload["project_id"])
        row = summaries.summarize_project(db, proj) if proj else None
        return {"summarised": bool(row)}
    if jt == RETENTION:
        return run_retention(db)
    if jt in SYNC_MODES:
        conn = db.get(SlackConnection, payload.get("connection_id"))
        if conn is None or conn.status != "connected":
            return {"ignored": True, "reason": "connection missing or not active"}
        heartbeat = lambda: setattr(job, "locked_at", utcnow())  # noqa: E731  keeps long syncs from looking crashed
        try:
            return sync_connection(db, conn, SYNC_MODES[jt], channel_id=payload.get("channel_id"), on_progress=heartbeat)
        finally:
            invalidate_user_caches(conn.owner_id)
    return {"ignored": True, "reason": f"unknown job type: {jt}"}


def run_discovery(db, owner_id: int) -> dict:
    res = projects.discover(db, owner_id)
    changed = res.pop("changed_ids", [])
    for i in range(0, min(len(changed), 500), 100):  # refresh vector/graph payloads that now carry a project
        rows = db.query(SlackMessage).filter(SlackMessage.id.in_(changed[i:i + 100]), SlackMessage.owner_id == owner_id).all()
        index_rows(db, rows)
    queued = 0
    for p in db.query(Project).filter_by(owner_id=owner_id, status="active").order_by(Project.message_count.desc()).limit(20).all():
        grown = p.message_count - (p.summary_count or 0)
        if p.message_count >= 5 and (not p.summary_text or grown >= max(5, int(0.25 * (p.summary_count or 0)))):
            queued += enqueue_job(db, SUMMARIZE_PROJECT, {"project_id": p.id}, dedupe_key=f"sum:{p.id}", owner_id=owner_id) is not None
            if queued >= 6:
                break
    db.commit()
    res["summaries_queued"] = queued
    return res


def run_retention(db) -> dict:
    """Storage lifecycle: P3 chatter outside any project expires early; everything expires eventually. Pending to-dos protect their message."""
    from sqlalchemy import and_, or_
    now = utcnow()
    p3_cut, all_cut = now - timedelta(days=settings.retain_p3_days), now - timedelta(days=settings.retain_all_days)
    ids = [i for (i,) in db.query(SlackMessage.id).filter(or_(and_(SlackMessage.priority_label == "P3", SlackMessage.project_id.is_(None),
           SlackMessage.message_created_at < p3_cut), SlackMessage.message_created_at < all_cut)).limit(2000).all()]
    if ids:
        protected = {i for (i,) in db.query(Reminder.message_id).filter(Reminder.message_id.in_(ids), Reminder.status == "pending").all()}
        protected |= {i for (i,) in db.query(SuggestedEvent.message_id).filter(SuggestedEvent.message_id.in_(ids), SuggestedEvent.status == "suggested").all()}
        ids = [i for i in ids if i not in protected]
    if ids:
        db.query(Reminder).filter(Reminder.message_id.in_(ids)).delete(synchronize_session=False)
        db.query(SuggestedEvent).filter(SuggestedEvent.message_id.in_(ids)).delete(synchronize_session=False)
        db.query(OutboundAction).filter(OutboundAction.message_id.in_(ids)).update({"message_id": None}, synchronize_session=False)
        db.query(SlackMessage).filter(SlackMessage.id.in_(ids)).delete(synchronize_session=False)
        integrations.delete_message_index(ids)
    old = db.query(PipelineEvent).filter(PipelineEvent.created_at < now - timedelta(days=settings.pipeline_event_retention_days)).delete(synchronize_session=False)
    log_event(db, "retention", "ok", f"expired {len(ids)} messages, {old} trace rows")
    db.commit()
    return {"expired_messages": len(ids), "trace_rows": old}


def beat(db, done: int) -> None:
    row = db.get(WorkerHeartbeat, "worker-1")
    if row is None:
        db.add(WorkerHeartbeat(name="worker-1", last_seen=utcnow(), jobs_done=done))
    else:
        row.last_seen, row.jobs_done = utcnow(), done
    db.commit()


def _finish(job, status, error=None):
    job.status, job.finished_at, job.dedupe_key = status, utcnow(), None  # release the dedupe slot
    if error is not None:
        job.last_error = error[:4000]


def handle_one() -> bool:
    with SessionLocal() as db:
        job_id = claim_job(db)
    if not job_id:
        return False
    with SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        if not job:
            return True
        try:
            result = dispatch(db, job, json.loads(job.payload_json))
            _finish(job, "completed")
            job.last_error = None
            db.commit()
            log.info("job_completed id=%s type=%s result=%s", job_id, job.job_type, result)
        except RescheduleJob as exc:
            db.rollback()
            job = db.get(ProcessingJob, job_id)
            job.status, job.attempts = "queued", max(0, job.attempts - 1)  # a rate limit is not a failure
            job.available_at = utcnow() + timedelta(seconds=exc.delay_seconds)
            job.last_error = str(exc)[:500]
            db.commit()
            log.warning("job_rescheduled id=%s type=%s in=%ss", job_id, job.job_type, exc.delay_seconds)
        except PermanentJobError as exc:
            db.rollback()
            job = db.get(ProcessingJob, job_id)
            _finish(job, "failed", str(exc))
            db.commit()
            log.error("job_failed_permanently id=%s type=%s error=%s", job_id, job.job_type, exc)
        except Exception as exc:
            db.rollback()
            job = db.get(ProcessingJob, job_id)
            if not job:
                return True
            if job.attempts >= job.max_attempts:
                _finish(job, "failed", str(exc))
            else:
                job.status, job.last_error = "queued", str(exc)[:4000]
                job.available_at = utcnow() + timedelta(seconds=min(300, 2 ** job.attempts))
            db.commit()
            log.exception("job_failed id=%s attempt=%s status=%s", job_id, job.attempts, job.status)
    return True


def run_pending_jobs(max_jobs: int = 5000, max_seconds: float | None = None) -> int:
    """Drain the queue in-process (used by the demo seeder and tests), optionally within a time budget."""
    n, t0 = 0, time.monotonic()
    while n < max_jobs and (max_seconds is None or time.monotonic() - t0 < max_seconds) and handle_one():
        n += 1
    return n


def recover_stuck_jobs(db) -> int:
    """A worker that died mid-job leaves it `processing` forever. Put those back (or fail them if attempts are spent)."""
    cutoff = utcnow() - timedelta(seconds=settings.job_lock_timeout_seconds)
    stuck = (db.query(ProcessingJob).filter(ProcessingJob.status == "processing", ProcessingJob.locked_at < cutoff)
             .with_for_update(skip_locked=True).all())
    for job in stuck:
        if job.attempts >= job.max_attempts:
            _finish(job, "failed", "worker crashed or timed out and attempts are exhausted")
        else:
            job.status, job.available_at, job.last_error = "queued", utcnow(), "recovered after worker crash/timeout"
    db.commit()
    if stuck:
        log.warning("recovered_stuck_jobs count=%s", len(stuck))
    return len(stuck)


def schedule_periodic(db) -> None:
    """Keep every connection fresh: incremental sync every N minutes, reconcile (edits/deletes/threads) every N hours."""
    now = utcnow()
    sync_every = timedelta(minutes=settings.slack_auto_sync_minutes)
    reconcile_every = timedelta(hours=settings.slack_reconcile_hours)
    for conn in db.query(SlackConnection).filter_by(status="connected", is_demo=False).all():
        if conn.sync_state in ("queued", "running"):
            continue
        last = as_utc(conn.last_sync_finished_at)
        if last is None or now - last > sync_every:
            enqueue_sync(db, conn, "auto")
        has_backfill = db.query(SlackConversation).filter_by(connection_id=conn.id, backfill_complete=True).first() is not None
        last_rec = as_utc(db.query(func.max(ProcessingJob.finished_at)).filter(
            ProcessingJob.job_type == SLACK_RECONCILE, ProcessingJob.connection_id == conn.id, ProcessingJob.status == "completed").scalar())
        if has_backfill and (last_rec is None or now - last_rec > reconcile_every):
            enqueue_sync(db, conn, "reconcile")
    for (owner_id,) in db.query(SlackConnection.owner_id).filter_by(status="connected").distinct().all():
        last_d = as_utc(db.query(func.max(ProcessingJob.finished_at)).filter(ProcessingJob.job_type == DISCOVER_PROJECTS, ProcessingJob.owner_id == owner_id,
                        ProcessingJob.status == "completed").scalar())
        if last_d is None or now - last_d > timedelta(minutes=10):  # discovery scans up to 30k rows: never more than every 10 min
            enqueue_job(db, DISCOVER_PROJECTS, {"owner_id": owner_id}, dedupe_key=f"discover:{owner_id}", owner_id=owner_id)
    last_ret = as_utc(db.query(func.max(ProcessingJob.finished_at)).filter(ProcessingJob.job_type == RETENTION, ProcessingJob.status == "completed").scalar())
    if last_ret is None or now - last_ret > timedelta(hours=24):
        enqueue_job(db, RETENTION, {}, dedupe_key="retention")
    db.commit()


def housekeeping() -> None:
    with SessionLocal() as db:
        recover_stuck_jobs(db)
        schedule_periodic(db)
        now = utcnow()
        db.query(UserSession).filter(UserSession.expires_at < now).delete()
        db.query(OAuthState).filter(OAuthState.expires_at < now - timedelta(hours=1)).delete()
        db.query(ProcessingJob).filter(ProcessingJob.status.in_(("completed", "failed")), ProcessingJob.finished_at < now - timedelta(days=7)).delete(synchronize_session=False)
        db.query(PipelineEvent).filter(PipelineEvent.created_at < now - timedelta(days=settings.pipeline_event_retention_days)).delete(synchronize_session=False)
        db.commit()


def main():
    init_db()
    log.info("ContextOS worker started; polling PostgreSQL queue")
    next_housekeeping, next_beat, done = 0.0, 0.0, 0
    while True:
        try:
            if time.monotonic() >= next_beat:
                with SessionLocal() as hb:
                    beat(hb, done)
                next_beat = time.monotonic() + 5
            if time.monotonic() >= next_housekeeping:
                try:
                    housekeeping()
                except Exception:
                    log.exception("housekeeping_error")
                next_housekeeping = time.monotonic() + 30
            if handle_one():
                done += 1
            else:
                time.sleep(max(0.2, settings.worker_poll_seconds))
        except KeyboardInterrupt:
            log.info("worker stopped")
            break
        except Exception:
            log.exception("worker_loop_error")
            time.sleep(2)


if __name__ == "__main__":
    main()
