"""Account lifecycle: delete my account and every byte of my data (also the 'right to be forgotten')."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import auth, integrations
from app.cache import invalidate_user_caches
from app.database import get_db
from app.models import (AssistantNote, Item, ItemEvent, ItemSource, LlmUsage, MessageProject, OAuthState, OutboundAction, PipelineEvent,
                        ProcessingJob, Project, GoogleCalendarConnection, ProjectKeyword, ProjectSummary, Reminder,
                        SlackConnection, SlackConversation, SlackMessage, SuggestedEvent, User, UserSession)
from app.pipeline import purge_connection_data
from app.slack_client import SlackClient
from app.slack_oauth import get_token, wipe_tokens

log = logging.getLogger("contextos.account")
router = APIRouter(tags=["account"])


class DeleteAccount(BaseModel):
    confirm_email: str = Field(min_length=3, max_length=320)


def delete_user_data(db: Session, user: User) -> dict:
    """Revoke provider tokens (best effort), then delete everything owned by the user. Does not delete the User row."""
    revoked = 0
    calendar_revoked = 0
    calendar_conn = db.query(GoogleCalendarConnection).filter_by(owner_id=user.id).first()
    if calendar_conn:
        from app import google_calendar
        calendar_revoked = int(google_calendar.revoke(calendar_conn))
        db.delete(calendar_conn)
    for conn in db.query(SlackConnection).filter_by(owner_id=user.id).all():
        if conn.status == "connected" and conn.provider == "gmail":
            from app import gmail
            revoked += int(gmail.revoke(db, conn))
        elif conn.status == "connected" and not conn.is_demo:
            try:
                with SlackClient(get_token(db, conn), max_inline_wait=2, retries=1) as client:
                    client.call("auth.revoke")
                revoked += 1
            except Exception as exc:
                log.info("account_delete_revoke_skipped reason=%s", type(exc).__name__)
        wipe_tokens(conn)
        purge_connection_data(db, conn)
        db.query(OutboundAction).filter(OutboundAction.connection_id == conn.id).delete(synchronize_session=False)
        db.query(ProcessingJob).filter(ProcessingJob.connection_id == conn.id).delete(synchronize_session=False)
    pids = [i for (i,) in db.query(Project.id).filter_by(owner_id=user.id).all()]
    db.query(ItemEvent).filter(ItemEvent.owner_id == user.id).delete(synchronize_session=False)
    db.query(ItemSource).filter(ItemSource.item_id.in_(db.query(Item.id).filter(Item.owner_id == user.id))).delete(synchronize_session=False)
    db.query(Item).filter(Item.owner_id == user.id).delete(synchronize_session=False)
    db.query(MessageProject).filter(MessageProject.owner_id == user.id).delete(synchronize_session=False)
    db.query(ProjectSummary).filter(ProjectSummary.owner_id == user.id).delete(synchronize_session=False)
    if pids:
        db.query(ProjectKeyword).filter(ProjectKeyword.project_id.in_(pids)).delete(synchronize_session=False)
    db.query(Project).filter(Project.owner_id == user.id).delete(synchronize_session=False)
    for model in (Reminder, SuggestedEvent, AssistantNote, OutboundAction, PipelineEvent, LlmUsage, ProcessingJob):
        db.query(model).filter(model.owner_id == user.id).delete(synchronize_session=False)
    db.query(SlackMessage).filter(SlackMessage.owner_id == user.id).delete(synchronize_session=False)
    db.query(SlackConversation).filter(SlackConversation.connection_id.in_(
        db.query(SlackConnection.id).filter_by(owner_id=user.id).scalar_subquery())).delete(synchronize_session=False)
    db.query(SlackConnection).filter_by(owner_id=user.id).delete(synchronize_session=False)
    db.query(OAuthState).filter_by(user_id=user.id).delete(synchronize_session=False)
    integrations.delete_summary_index(pids)
    return {"slack_tokens_revoked": revoked, "google_calendar_token_revoked": bool(calendar_revoked), "projects_deleted": len(pids)}


@router.delete("/api/account")
def delete_account(data: DeleteAccount, response: Response, ctx: auth.AuthContext = Depends(auth.auth_context), db: Session = Depends(get_db)):
    user = ctx.user
    if auth.normalize_email(data.confirm_email) != user.email:
        raise HTTPException(status_code=400, detail={"code": "confirm_mismatch", "message": "Type your account email exactly to confirm."})
    report = delete_user_data(db, user)
    db.query(UserSession).filter_by(user_id=user.id).delete(synchronize_session=False)
    db.delete(db.get(User, user.id))
    db.commit()
    invalidate_user_caches(user.id)
    auth.clear_session_cookie(response)
    return {"ok": True, **report}
