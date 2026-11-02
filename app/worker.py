"""Run with `python -m app.worker` in a separate terminal (run as many as you like).

PostgreSQL is the durable queue of record. Claiming uses FOR UPDATE SKIP LOCKED so multiple workers are safe.
Job types: slack_event, slack_process_message, slack_initial_sync, slack_incremental_sync, slack_reconcile_conversation,
project_discover, project_summarize, index_backfill, retention.
Resilience: exponential backoff, rate-limit reschedule (no attempt consumed; Slack AND LLM limits), permanent-failure
short-circuit, recovery of jobs stranded in `processing` by a crash, graceful SIGTERM/SIGINT (finish the current job, then
exit), a heartbeat the UI shows, and a scheduler that keeps every connection syncing and every project summarised.
"""
import json
import logging
import os
import signal
import socket
import time
from datetime import timedelta

from sqlalchemy import func, or_, select

from app import cache, intelligence, llm, pipeline_events, projects, retention, summaries
from app.cache import invalidate_user_caches
from app.config import settings
from app.database import SessionLocal, init_db
from app.jobs import (INDEX_BACKFILL, PROJECT_DISCOVER, PROJECT_SUMMARIZE, PermanentJobError, RETENTION, RescheduleJob, SLACK_EVENT,
                      SLACK_INCREMENTAL_SYNC, SLACK_INITIAL_SYNC, SLACK_PROCESS_MESSAGE, SLACK_RECONCILE, enqueue_job)
from app.models import (OAuthState, ProcessingJob, Project, SlackConnection, SlackConversation, SlackMessage, UserSession, WorkerHeartbeat, as_utc,
                        utcnow)
from app.slack_events import process_message_job, process_slack_event
from app.slack_sync import enqueue_sync, sync_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("contextos.worker")

SYNC_MODES = {SLACK_INITIAL_SYNC: "initial", SLACK_INCREMENTAL_SYNC: "incremental", SLACK_RECONCILE: "reconcile"}
INGEST_JOBS = (SLACK_EVENT, SLACK_PROCESS_MESSAGE, SLACK_INITIAL_SYNC, SLACK_INCREMENTAL_SYNC)
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_stop = False
_jobs_done = 0


def _bump_jobs() -> None:
    global _jobs_done
    _jobs_done += 1


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


def _ingestion_pending(db, owner_id: int) -> int:
    """Queued/processing ingestion work for one user (their sync jobs, and workspace events for their workspaces)."""
    conn_ids = [i for (i,) in db.query(SlackConnection.id).filter_by(owner_id=owner_id).all()]
    teams = [t for (t,) in db.query(SlackConnection.team_id).filter_by(owner_id=owner_id).all()]
    conds = []
    if conn_ids:
        conds.append(ProcessingJob.connection_id.in_(conn_ids))
    if teams:
        conds.append(ProcessingJob.team_id.in_(teams))
    if not conds:
        return 0
    return (db.query(func.count(ProcessingJob.id)).filter(ProcessingJob.status.in_(("queued", "processing")), ProcessingJob.job_type.in_(INGEST_JOBS),
                                                          or_(*conds)).scalar() or 0)


def _derived_signals(db, owner_id: int) -> dict:
    """Qdrant + Neo4j driven refinements. Each is optional and isolated: a failure here never fails discovery."""
    out = {}
    for name, fn in (("semantic", projects.semantic_link), ("collaborators", projects.apply_people_boost),
                     ("echo", intelligence.apply_semantic_escalation)):
        try:
            out[name] = fn(db, owner_id)
        except Exception as exc:
            db.rollback()
            log.warning("derived_signal_failed name=%s %s", name, type(exc).__name__)
            out[name] = {"error": type(exc).__name__}
    return out


def _discover(db, job, payload):
    owner_id = payload["owner_id"]
    waited = utcnow() - as_utc(job.created_at)
    pending = _ingestion_pending(db, owner_id)
    if pending and waited < timedelta(minutes=10):  # let ingestion finish first so discovery sees the whole picture
        raise RescheduleJob(10, f"waiting for {pending} ingestion job(s)")
    report = projects.discover_projects(db, owner_id)
    ext = projects.sync_external_indexes(db, owner_id) if report.get("projects") else {}
    derived = _derived_signals(db, owner_id) if report.get("projects") else {}
    queued = 0
    for pid, dirty in db.query(Project.id, Project.summary_dirty).filter(Project.owner_id == owner_id, Project.status.in_(("auto", "confirmed"))).all():
        if dirty and projects.enqueue_summary(db, pid, owner_id=owner_id) is not None:
            queued += 1
    db.commit()
    invalidate_user_caches(owner_id)
    return {**report, "summaries_queued": queued, "external": ext, "derived": derived}


def _summarize(db, job, payload):
    try:
        res = summaries.summarize_project(db, payload["project_id"], force=bool(payload.get("force")), raise_on_rate_limit=True)
    except llm.LLMRateLimited as exc:  # free-tier limit: wait it out, do not burn a retry attempt
        raise RescheduleJob(min(3600, exc.retry_after + 2), f"LLM rate limited ({exc.scope})") from exc
    owner = db.query(Project.owner_id).filter(Project.id == payload["project_id"]).scalar()
    if owner:
        if res.get("version"):  # a fresh summary vector now exists: messages no keyword caught can be linked by meaning
            try:
                projects.semantic_link(db, owner)
            except Exception as exc:
                db.rollback()
                log.warning("semantic_link_failed %s", type(exc).__name__)
        invalidate_user_caches(owner)
    return res


def dispatch(db, job, payload):
    jt = job.job_type
    if jt == PROJECT_DISCOVER:
        return _discover(db, job, payload)
    if jt == PROJECT_SUMMARIZE:
        return _summarize(db, job, payload)
    if jt == RETENTION:
        return retention.run_retention(db)
    if jt == INDEX_BACKFILL:
        owner = payload["owner_id"]
        res = intelligence.backfill(db, owner, force_graph=bool(payload.get("force_graph")))
        invalidate_user_caches(owner)
        return res
    if jt == SLACK_EVENT:
        return process_slack_event(db, payload)
    if jt == SLACK_PROCESS_MESSAGE:
        return process_message_job(db, payload)
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
            _bump_jobs()
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


def run_pending_jobs(max_jobs: int = 5000, wait_seconds: float = 0.0) -> int:
    """Drain the queue in-process (demo seeder, tests, and the 'no worker running' fallback).
    wait_seconds > 0 also waits for jobs that were rescheduled a few seconds into the future (e.g. discovery waiting
    for ingestion), up to that long in total."""
    n, deadline = 0, time.monotonic() + wait_seconds
    while n < max_jobs:
        if handle_one():
            n += 1
            continue
        if time.monotonic() >= deadline:
            break
        with SessionLocal() as db:
            soon = (db.query(func.count(ProcessingJob.id)).filter(ProcessingJob.status == "queued",
                    ProcessingJob.available_at <= utcnow() + timedelta(seconds=15)).scalar() or 0)
        if not soon:
            break
        time.sleep(0.5)
    return n


def worker_online(db, within_seconds: int = 90) -> bool:
    cutoff = utcnow() - timedelta(seconds=within_seconds)
    return db.query(func.count(WorkerHeartbeat.worker_id)).filter(WorkerHeartbeat.beat_at > cutoff).scalar() > 0


def beat(db) -> None:
    row = db.get(WorkerHeartbeat, WORKER_ID)
    if row is None:
        db.add(WorkerHeartbeat(worker_id=WORKER_ID, beat_at=utcnow(), jobs_done=_jobs_done))
    else:
        row.beat_at, row.jobs_done = utcnow(), _jobs_done
    db.query(WorkerHeartbeat).filter(WorkerHeartbeat.beat_at < utcnow() - timedelta(days=1)).delete(synchronize_session=False)


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
    db.commit()


_last_summary_scan = 0.0
_last_prune = 0.0


def schedule_intelligence(db) -> None:
    """Keep project summaries fresh (dirty + not recently summarised, or upgradable from extractive) and run retention daily."""
    global _last_summary_scan
    if time.monotonic() - _last_summary_scan > 60:
        _last_summary_scan = time.monotonic()
        for pid in summaries.due_projects(db):
            projects.enqueue_summary(db, pid)
        db.commit()
    last = as_utc(db.query(func.max(ProcessingJob.finished_at)).filter(ProcessingJob.job_type == RETENTION, ProcessingJob.status == "completed").scalar())
    if settings.retention_enabled and (last is None or utcnow() - last > timedelta(hours=24)) and cache.acquire_lock("retention", 300):
        enqueue_job(db, RETENTION, {}, dedupe_key="retention", max_attempts=2)
        db.commit()


def schedule_index_backfill(db) -> None:
    """Qdrant / Neo4j added (or recovered) after messages already existed? Catch them up, at most once an hour per user.
    The job itself does nothing when the indexes are already complete."""
    if not (settings.qdrant_enabled or settings.neo4j_enabled):
        return
    now = utcnow()
    owners = [o for (o,) in db.query(SlackMessage.owner_id).filter(SlackMessage.is_deleted.is_(False), SlackMessage.priority_label != "P3").distinct().limit(500).all()]
    for owner in owners:
        last = as_utc(db.query(func.max(ProcessingJob.created_at)).filter(ProcessingJob.job_type == INDEX_BACKFILL, ProcessingJob.owner_id == owner).scalar())
        if last is None or now - last > timedelta(hours=1):
            enqueue_job(db, INDEX_BACKFILL, {"owner_id": owner}, owner_id=owner, dedupe_key=f"index_backfill:{owner}", max_attempts=2)
    db.commit()


def housekeeping() -> None:
    global _last_prune
    with SessionLocal() as db:
        beat(db)
        db.commit()
        recover_stuck_jobs(db)
        schedule_periodic(db)
        schedule_intelligence(db)
        schedule_index_backfill(db)
        if time.monotonic() - _last_prune > 600:
            _last_prune = time.monotonic()
            pipeline_events.prune(db)
            db.commit()
        now = utcnow()
        db.query(UserSession).filter(UserSession.expires_at < now).delete()
        db.query(OAuthState).filter(OAuthState.expires_at < now - timedelta(hours=1)).delete()
        db.query(ProcessingJob).filter(ProcessingJob.status.in_(("completed", "failed")), ProcessingJob.finished_at < now - timedelta(days=7)).delete(synchronize_session=False)
        db.commit()


def _request_stop(signum, frame):
    global _stop
    _stop = True
    log.info("shutdown requested (signal %s): finishing the current job, then exiting", signum)


def main():
    init_db()
    for line in settings.production_problems():
        log.warning(line)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _request_stop)
    log.info("ContextOS worker %s started; polling PostgreSQL queue", WORKER_ID)
    next_housekeeping = 0.0
    while not _stop:
        try:
            if time.monotonic() >= next_housekeeping:
                try:
                    housekeeping()
                except Exception:
                    log.exception("housekeeping_error")
                next_housekeeping = time.monotonic() + 30
            if not handle_one():
                time.sleep(max(0.2, settings.worker_poll_seconds))
        except Exception:
            log.exception("worker_loop_error")
            time.sleep(2)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
