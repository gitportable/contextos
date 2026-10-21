"""Pipeline trace: one row per layer a request/message passes through. Powers the Data Pipeline page."""
from sqlalchemy.orm import Session

from app.models import PipelineEvent


def log_event(db: Session, stage: str, status: str = "ok", detail: str = "", *, owner_id: int | None = None,
              team_id: str | None = None, message_id: int | None = None) -> None:
    """Adds a row to the CALLER's transaction (no commit), so a trace entry exists iff the work it describes committed."""
    db.add(PipelineEvent(owner_id=owner_id, team_id=team_id, message_id=message_id, stage=stage, status=status, detail=detail[:500]))
