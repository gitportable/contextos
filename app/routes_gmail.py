"""Connect / reconnect a Gmail account (read-only). Status, sync and disconnect reuse the /integrations/slack/* endpoints,
because a Gmail account is stored as a connection with provider='gmail'."""
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import gmail, pipeline_events, ratelimit
from app.auth import AuthContext, optional_context
from app.cache import invalidate_user_caches
from app.database import get_db
from app.slack_sync import enqueue_sync

log = logging.getLogger("contextos.gmail_routes")
router = APIRouter(prefix="/integrations/gmail", tags=["gmail"])


@router.get("/connect")
def connect(ctx: AuthContext = Depends(optional_context), db: Session = Depends(get_db)):
    if ctx is None:
        raise HTTPException(status_code=401, detail={"code": "not_authenticated", "message": "Sign in to ContextOS first."})
    try:
        ratelimit.hit("oauth:user", str(ctx.user.id), 20, 3600, "Too many connection attempts. Try again later.")
        url = gmail.authorize_url(gmail.create_state(db, ctx.user), hint=ctx.user.email)
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    except gmail.GmailError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
def callback(code: str | None = None, state: str | None = None, error: str | None = None,
             ctx: AuthContext | None = Depends(optional_context), db: Session = Depends(get_db)):
    def back(status: str, reason: str = "") -> RedirectResponse:
        return RedirectResponse(f"/?gmail={status}" + (f"&reason={quote(reason)}" if reason else ""), status_code=302)

    if error:
        return back("denied", error)
    if ctx is None:
        return back("error", "not_logged_in")
    try:
        gmail.consume_state(db, state, ctx.user)
        if not code:
            raise gmail.GmailError("missing_code")
        conn, _ = gmail.save_connection(db, ctx.user, code)
    except gmail.GmailError as exc:
        log.warning("gmail_oauth_failed user=%s code=%s", ctx.user.id, exc.code)
        return back("error", exc.code)
    enqueue_sync(db, conn, "initial")
    pipeline_events.record(db, "slack_connected", "ok", owner_id=ctx.user.id, team_id=conn.team_id,
                           detail=f"Gmail {conn.team_name} connected (read-only)")
    db.commit()
    invalidate_user_caches(ctx.user.id)
    return back("connected")
