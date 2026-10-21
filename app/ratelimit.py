"""Rate limiting and login lockout.

Counters live in Redis when configured (fast, shared) and in PostgreSQL otherwise (durable, shared across workers).
The DB backend uses its OWN session, so recording a failed login can never commit/rollback a caller's transaction.
Security counters FAIL CLOSED for lockouts (they are DB-backed) and FAIL OPEN for generic API limits if Redis dies.
"""
import logging
from datetime import timedelta

from app import cache
from app.config import settings
from app.database import SessionLocal
from app.models import RateLimit, as_utc, utcnow

log = logging.getLogger("contextos.ratelimit")


def hit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Count one request. Returns (allowed, retry_after_seconds)."""
    r = cache.client()
    if r:
        try:
            full = f"contextos:rl:{key}"
            pipe = r.pipeline()
            pipe.incr(full)
            pipe.ttl(full)
            count, ttl = pipe.execute()
            if ttl < 0:
                r.expire(full, window_seconds); ttl = window_seconds
            return (count <= limit), (0 if count <= limit else max(1, ttl))
        except Exception:
            log.warning("redis rate limit unavailable; using database")
    with SessionLocal() as db:
        now = utcnow()
        row = db.query(RateLimit).filter_by(key=key).first()
        if row is None:
            try:
                db.add(RateLimit(key=key, count=0, window_start=now)); db.commit()
            except Exception:
                db.rollback()
            row = db.query(RateLimit).filter_by(key=key).first()
        if now - as_utc(row.window_start) > timedelta(seconds=window_seconds):
            db.query(RateLimit).filter_by(key=key).update({"count": 0, "window_start": now})
            db.commit()
            row = db.query(RateLimit).filter_by(key=key).first()
        db.query(RateLimit).filter_by(key=key).update({RateLimit.count: RateLimit.count + 1})  # atomic increment
        db.commit()
        count = db.query(RateLimit.count).filter_by(key=key).scalar()
        retry = max(1, int(window_seconds - (now - as_utc(row.window_start)).total_seconds()))
        return (count <= limit), (0 if count <= limit else retry)


def peek(key: str) -> int:
    r = cache.client()
    if r:
        try:
            return int(r.get(f"contextos:rl:{key}") or 0)
        except Exception:
            pass
    with SessionLocal() as db:
        return db.query(RateLimit.count).filter_by(key=key).scalar() or 0


# ------------------------------------------------------------- login guard ---
def lock_remaining(*keys: str) -> int:
    """Seconds until the earliest of these keys unlocks (0 if none is locked)."""
    with SessionLocal() as db:
        now, worst = utcnow(), 0
        for k in keys:
            row = db.query(RateLimit).filter_by(key=k).first()
            if row and row.locked_until and as_utc(row.locked_until) > now:
                worst = max(worst, int((as_utc(row.locked_until) - now).total_seconds()) + 1)
        return worst


def record_failure(key: str, max_attempts: int) -> tuple[int, int]:
    """Register a failed attempt. Returns (attempts_remaining, lock_seconds). Lockouts double (cap 24 h)."""
    with SessionLocal() as db:
        now = utcnow()
        row = db.query(RateLimit).filter_by(key=key).first()
        if row is None:
            try:
                db.add(RateLimit(key=key, count=0, window_start=now)); db.commit()
            except Exception:
                db.rollback()
            row = db.query(RateLimit).filter_by(key=key).first()
        if now - as_utc(row.window_start) > timedelta(seconds=settings.login_window_seconds):
            db.query(RateLimit).filter_by(key=key).update({"count": 0, "window_start": now}); db.commit()
        db.query(RateLimit).filter_by(key=key).update({RateLimit.count: RateLimit.count + 1}); db.commit()
        row = db.query(RateLimit).filter_by(key=key).first()
        if row.count >= max_attempts:
            lock = min(86400, settings.login_lockout_seconds * (2 ** row.strikes))
            db.query(RateLimit).filter_by(key=key).update(
                {"locked_until": now + timedelta(seconds=lock), "strikes": RateLimit.strikes + 1, "count": 0, "window_start": now})
            db.commit()
            return 0, lock
        return max(0, max_attempts - row.count), 0


def clear_failures(key: str) -> None:
    with SessionLocal() as db:
        db.query(RateLimit).filter_by(key=key).update({"count": 0, "strikes": 0, "locked_until": None})
        db.commit()
