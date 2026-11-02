"""Connect and disconnect a user's read-only Google Calendar."""
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import google_calendar, ratelimit
from app.auth import AuthContext, current_user, optional_context
from app.cache import invalidate_user_caches
from app.config import settings
from app.database import get_db
from app.models import GoogleCalendarConnection, User

log = logging.getLogger("contextos.google_calendar_routes")
router = APIRouter(prefix="/integrations/google-calendar", tags=["google-calendar"])


@router.get("/connect")
def connect(ctx: AuthContext = Depends(optional_context), db: Session = Depends(get_db)):
    if ctx is None:
        raise HTTPException(status_code=401, detail={"code": "not_authenticated", "message": "Sign in to ContextOS first."})
    try:
        ratelimit.hit("oauth:user", str(ctx.user.id), 20, 3600, "Too many connection attempts. Try again later.")
        url = google_calendar.create_state(db, ctx.user)
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    except google_calendar.GoogleCalendarError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
def callback(code: str | None = None, state: str | None = None, error: str | None = None,
             ctx: AuthContext | None = Depends(optional_context), db: Session = Depends(get_db)):
    def back(status: str, reason: str = "") -> RedirectResponse:
        return RedirectResponse(f"/?google_calendar={status}" + (f"&reason={quote(reason)}" if reason else ""), status_code=302)

    if error:
        return back("denied", error)
    if ctx is None:
        return back("error", "not_logged_in")
    if not code:
        return back("error", "missing_code")
    try:
        google_calendar.save_connection(db, ctx.user, code, state or "")
    except google_calendar.GoogleCalendarError as exc:
        log.warning("google_calendar_oauth_failed user=%s code=%s", ctx.user.id, exc.code)
        return back("error", exc.code)
    invalidate_user_caches(ctx.user.id)
    return back("connected")


@router.get("/connection")
def connection_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = google_calendar.get_connection(db, user.id)
    return {
        "configured": settings.google_oauth_configured and bool(settings.token_encryption_key.strip()),
        "connected": bool(conn and conn.status == "connected"),
        "email": conn.email if conn and conn.status == "connected" else None,
        "status": conn.status if conn else "disconnected",
    }


@router.get("/events/today")
def today_events(user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        return {"events": google_calendar.events_today(db, user.id) or []}
    except google_calendar.GoogleCalendarError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/disconnect")
def disconnect(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = google_calendar.get_connection(db, user.id)
    if conn is None:
        return {"ok": True, "revoked": True}
    revoked = google_calendar.revoke(conn)
    db.delete(conn)
    db.commit()
    invalidate_user_caches(user.id)
    return {"ok": True, "revoked": revoked}
