"""Optional Redis. PostgreSQL stays the source of truth; Redis only ever holds things that are safe to lose:
response cache, rate-limit counters, LLM quota counters, short locks. A Redis outage degrades features, never data.

If Redis goes down we stop calling it for COOLDOWN seconds (so requests don't each wait for a timeout), then try again.
"""
import json
import logging
import threading
import time

from app.config import settings

log = logging.getLogger("contextos.cache")
COOLDOWN = 60.0
_lock = threading.Lock()
_client = None
_down_until = 0.0


def _connect():
    import redis
    c = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=0.25, socket_timeout=0.25, retry_on_timeout=False, health_check_interval=30)
    c.ping()
    return c


def client():
    """A live Redis client, or None (not configured, or inside the cool-down after a failure)."""
    global _client, _down_until
    if not settings.redis_url:
        return None
    if time.monotonic() < _down_until:
        return None
    if _client is not None:
        return _client
    with _lock:
        if _client is None and time.monotonic() >= _down_until:
            try:
                _client = _connect()
            except Exception as exc:
                log.warning(
                    "redis_unavailable %s: %s; retrying in %ss",
                    type(exc).__name__,
                    str(exc)[:300],
                    int(COOLDOWN),
                )
                # log.warning("redis_unavailable %s; retrying in %ss", type(exc).__name__, int(COOLDOWN))
                # _down_until = time.monotonic() + COOLDOWN
        return _client


def mark_down() -> None:
    """Callers invoke this when a command failed: drop the connection and back off."""
    global _client, _down_until
    _client = None
    _down_until = time.monotonic() + COOLDOWN


def reset_for_tests() -> None:
    global _client, _down_until
    _client, _down_until = None, 0.0


def get_json(key):
    c = client()
    if not c:
        return None
    try:
        value = c.get(key)
        return json.loads(value) if value else None
    except Exception:
        mark_down()
        return None


def set_json(key, value, ttl=15):
    c = client()
    if not c:
        return
    try:
        c.setex(key, ttl, json.dumps(value, default=str))
    except Exception:
        mark_down()


def _delete_pattern(pattern: str) -> None:
    c = client()
    if not c:
        return
    try:
        batch = []
        for key in c.scan_iter(pattern, count=500):
            batch.append(key)
            if len(batch) >= 500:
                c.delete(*batch)
                batch = []
        if batch:
            c.delete(*batch)
    except Exception:
        mark_down()


def invalidate_user_caches(owner_id):
    """Cache keys are namespaced per ContextOS user so one user's data can never be served to another."""
    _delete_pattern(f"contextos:u{owner_id}:*")


def invalidate_message_caches():  # backwards-compatible: drop everything
    _delete_pattern("contextos:u*")


def acquire_lock(name: str, ttl: int = 60) -> bool:
    """Best-effort mutual exclusion (SET NX EX). Without Redis it always succeeds: correctness never depends on it,
    the real guards are the PostgreSQL dedupe_key and FOR UPDATE SKIP LOCKED. The lock only avoids duplicate work."""
    c = client()
    if not c:
        return True
    try:
        return bool(c.set(f"contextos:lock:{name}", "1", nx=True, ex=ttl))
    except Exception:
        mark_down()
        return True


def release_lock(name: str) -> None:
    c = client()
    if c:
        try:
            c.delete(f"contextos:lock:{name}")
        except Exception:
            mark_down()


def progress_set(owner_id: int, key: str, value: int, ttl: int = 3600) -> None:
    """Sync-progress counters shown on the pipeline page (cheap, disposable)."""
    c = client()
    if c:
        try:
            c.setex(f"contextos:progress:{owner_id}:{key}", ttl, value)
        except Exception:
            mark_down()


def progress_get(owner_id: int, key: str) -> int | None:
    c = client()
    if not c:
        return None
    try:
        v = c.get(f"contextos:progress:{owner_id}:{key}")
        return int(v) if v is not None else None
    except Exception:
        mark_down()
        return None
