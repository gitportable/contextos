"""Queue helpers shared by the API, the worker, sync and the demo."""
import json
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ProcessingJob, utcnow

SLACK_EVENT = "slack_event"
SLACK_PROCESS_MESSAGE = "slack_process_message"
SLACK_INITIAL_SYNC = "slack_initial_sync"
SLACK_INCREMENTAL_SYNC = "slack_incremental_sync"
SLACK_RECONCILE = "slack_reconcile_conversation"
PROJECT_DISCOVER = "project_discover"
PROJECT_SUMMARIZE = "project_summarize"
RETENTION = "retention"
INDEX_BACKFILL = "index_backfill"


class PermanentJobError(Exception):
    """Do not retry (e.g. revoked token). The job is marked failed immediately."""


class RescheduleJob(Exception):
    """Run again later WITHOUT consuming an attempt (Slack rate limit). Progress made so far is already committed."""
    def __init__(self, delay_seconds: int, reason: str = ""):
        super().__init__(reason or f"rescheduled in {delay_seconds}s")
        self.delay_seconds = delay_seconds


def enqueue_job(db: Session, job_type: str, payload: dict, *, connection_id: int | None = None, owner_id: int | None = None,
                dedupe_key: str | None = None, delay_seconds: int = 0, max_attempts: int = 5) -> ProcessingJob | None:
    """Add a job inside a savepoint. Returns None if an identical (dedupe_key) job is already queued/processing.
    The caller commits."""
    job = ProcessingJob(job_type=job_type, payload_json=json.dumps(payload), connection_id=connection_id, owner_id=owner_id, dedupe_key=dedupe_key,
                        status="queued", max_attempts=max_attempts, available_at=utcnow() + timedelta(seconds=delay_seconds))
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        return None
    return job
