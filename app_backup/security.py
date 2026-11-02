"""HTTP hardening: security headers, strict CSP, Host/Origin checks, body-size cap, safe error responses."""
import logging
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import settings

log = logging.getLogger("contextos.security")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def _hosts() -> set[str]:
    base = urlparse(settings.public_base_url).hostname or "localhost"
    return {base, "localhost", "127.0.0.1", "testserver"}


def install(app: FastAPI) -> None:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(_hosts()))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in UNSAFE:
            cl = request.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > settings.max_body_bytes:
                return JSONResponse({"detail": {"code": "too_large", "message": "Request body too large."}}, status_code=413)
            origin = request.headers.get("origin")
            if origin and request.url.path != "/integrations/slack/events":
                if (urlparse(origin).hostname or "") not in _hosts():  # cross-site form/fetch carrying our cookies
                    return JSONResponse({"detail": {"code": "bad_origin", "message": "Cross-origin request blocked."}}, status_code=403)
        response = await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if settings.cookie_secure:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith(("/api", "/auth", "/messages", "/integrations", "/queue", "/reminders", "/schedule", "/actions", "/dashboard")):
            h["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled_error path=%s", request.url.path)  # details go to logs, never to the client
        return JSONResponse({"detail": {"code": "server_error", "message": "Something went wrong on our side. Please try again."}}, status_code=500)
