"""Rate limiting + login lockout.

Backend: Redis when REDIS_URL works (shared by every worker/process, survives restarts); otherwise an in-process
fallback so limits still apply (fail CLOSED to a weaker limiter, never open). The fallback is per-process and resets
on restart: production_problems() warns when REDIS_URL is empty.

Counters use INCR + TTL repair, so a crash between INCR and EXPIRE cannot leave an immortal key (which would be a
permanent lock-out).
"""
import hashlib
import ipaddress
import logging
import math
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app import cache
from app.config import settings

log = logging.getLogger("contextos.ratelimit")


class RateLimited(Exception):
    def __init__(self, retry_after: int, scope: str, message: str = ""):
        super().__init__(message or f"rate limited: {scope}")
        self.retry_after, self.scope = max(1, int(math.ceil(retry_after))), scope
        self.message = message or "Too many requests. Please wait a moment and try again."


@dataclass
class Hit:
    count: int
    ttl: int


class _Memory:
    """Bounded in-process fallback store: key -> (count, expires_at_monotonic)."""
    MAX_KEYS = 20000

    def __init__(self):
        self._d: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        if len(self._d) > self.MAX_KEYS:
            for k in [k for k, (_, exp) in self._d.items() if exp <= now][: self.MAX_KEYS // 2]:
                self._d.pop(k, None)
            while len(self._d) > self.MAX_KEYS:  # still full of live keys: drop the oldest-expiring
                self._d.pop(min(self._d, key=lambda k: self._d[k][1]), None)

    def incr(self, key: str, window: int) -> Hit:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            count, exp = self._d.get(key, (0, 0.0))
            if exp <= now:
                count, exp = 0, now + window
            count += 1
            self._d[key] = (count, exp)
            return Hit(count, int(math.ceil(exp - now)))

    def get(self, key: str) -> Hit:
        now = time.monotonic()
        with self._lock:
            count, exp = self._d.get(key, (0, 0.0))
            return Hit(count, int(math.ceil(exp - now))) if exp > now else Hit(0, 0)

    def set(self, key: str, count: int, ttl: int) -> None:
        with self._lock:
            self._d[key] = (count, time.monotonic() + ttl)

    def delete(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._d.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


_mem = _Memory()


def reset_for_tests() -> None:
    _mem.clear()


def _incr(key: str, window: int) -> Hit:
    c = cache.client()
    if c:
        try:
            pipe = c.pipeline()
            pipe.incr(key)
            pipe.ttl(key)
            count, ttl = pipe.execute()
            if ttl is None or ttl < 0:  # first hit, or TTL lost
                c.expire(key, window)
                ttl = window
            return Hit(int(count), int(ttl))
        except Exception as exc:
            log.warning("redis_ratelimit_error %s; using in-process fallback", type(exc).__name__)
            cache.mark_down()
    return _mem.incr(key, window)


def _peek(key: str) -> Hit:
    c = cache.client()
    if c:
        try:
            pipe = c.pipeline()
            pipe.get(key)
            pipe.ttl(key)
            v, ttl = pipe.execute()
            return Hit(int(v or 0), max(0, int(ttl or 0)))
        except Exception:
            cache.mark_down()
    return _mem.get(key)


def _set(key: str, count: int, ttl: int) -> None:
    c = cache.client()
    if c:
        try:
            c.setex(key, ttl, count)
            return
        except Exception:
            cache.mark_down()
    _mem.set(key, count, ttl)


def _delete(*keys: str) -> None:
    c = cache.client()
    if c:
        try:
            c.delete(*keys)
        except Exception:
            cache.mark_down()
    _mem.delete(*keys)


def _h(value: str) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()[:20]


# ----------------------------------------------------------------- generic ---
def hit(scope: str, ident: str, limit: int, window: int, message: str = "") -> Hit:
    """Count one event; raise RateLimited when the window's budget is spent."""
    res = _incr(f"rl:{scope}:{ident}", window)
    if res.count > limit:
        raise RateLimited(res.ttl or window, scope, message)
    return res


def client_ip(request: Request) -> str:
    """Peer address, or the Nth-from-right X-Forwarded-For entry when TRUSTED_PROXY_HOPS>0.
    Taking entries from the RIGHT means a client cannot forge its own address by sending its own header."""
    peer = request.client.host if request.client else "unknown"
    hops = settings.trusted_proxy_hops
    if hops > 0:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= hops:
            cand = parts[-hops]
            try:
                return str(ipaddress.ip_address(cand))
            except ValueError:
                pass
    return peer


# ------------------------------------------------------------------- login ---
def login_ip_gate(ip: str) -> None:
    """Counts EVERY password sign-in attempt (even malformed ones) against the caller's network address."""
    hit("login:ip", ip, settings.rl_login_ip_per_5min, 300, "Too many sign-in attempts from this network. Wait a few minutes.")


def login_account_gate(email: str, ip: str) -> None:
    """Call BEFORE touching the database/password hash. Raises RateLimited."""
    lock = _peek(f"rl:lock:{_h(email)}:{_h(ip)}")
    if lock.ttl > 0 and lock.count > 0:
        raise RateLimited(lock.ttl, "login:lock", f"Too many failed sign-ins. Try again in {_fmt(lock.ttl)}, or continue with Google.")
    em = _peek(f"rl:login:email:{_h(email)}")
    if em.count >= settings.rl_login_email_fail_per_hour:
        raise RateLimited(em.ttl or 3600, "login:email", "This account is temporarily protected from password guessing. Continue with Google or try again later.")


def login_precheck(email: str, ip: str) -> None:
    login_ip_gate(ip)
    login_account_gate(email, ip)


def login_failed(email: str, ip: str) -> None:
    n = _incr(f"rl:login:pair:{_h(email)}:{_h(ip)}", 900).count
    _incr(f"rl:login:email:{_h(email)}", 3600)
    over = n - settings.rl_login_fail_lock_after
    if over >= 0:
        _set(f"rl:lock:{_h(email)}:{_h(ip)}", n, min(3600, 60 * (2 ** over)))  # 1, 2, 4, 8 ... min, capped at 1 h


def login_succeeded(email: str, ip: str) -> None:
    _delete(f"rl:login:pair:{_h(email)}:{_h(ip)}", f"rl:lock:{_h(email)}:{_h(ip)}")


def _fmt(seconds: int) -> str:
    return f"{seconds // 60 + 1} minute(s)" if seconds >= 60 else f"{seconds} second(s)"


# --------------------------------------------------------- FastAPI helpers ---
def limit_ip(scope: str, limit: int, window: int):
    """Dependency factory: per-IP limit for unauthenticated endpoints."""
    def dep(request: Request):
        hit(scope, client_ip(request), limit, window)
    return dep


def rate_limit_response(exc: RateLimited):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=429, headers={"Retry-After": str(exc.retry_after)},
                        content={"detail": {"code": "rate_limited", "message": exc.message, "retry_after": exc.retry_after, "scope": exc.scope}})


def ensure_http(exc: RateLimited) -> HTTPException:
    return HTTPException(status_code=429, headers={"Retry-After": str(exc.retry_after)},
                         detail={"code": "rate_limited", "message": exc.message, "retry_after": exc.retry_after})
