"""Optional Redis cache. PostgreSQL remains the source of truth; cache failure never blocks ingestion."""
import json, logging
from app.config import settings
log=logging.getLogger("contextos.cache")
_client=None

def client():
    global _client
    if not settings.redis_url: return None
    if _client is None:
        try:
            import redis
            _client=redis.Redis.from_url(settings.redis_url,decode_responses=True,socket_connect_timeout=1,socket_timeout=1)
            _client.ping()
        except Exception as exc:
            log.warning("Redis unavailable; bypassing cache: %s",type(exc).__name__); _client=False
    return _client if _client is not False else None

def get_json(key):
    c=client()
    if not c: return None
    try:
        value=c.get(key)
        return json.loads(value) if value else None
    except Exception: return None

def set_json(key,value,ttl=15):
    c=client()
    if not c: return
    try: c.setex(key,ttl,json.dumps(value,default=str))
    except Exception: pass

def invalidate_user_caches(owner_id):
    """Cache keys are namespaced per ContextOS user so one user's data can never be served to another."""
    c=client()
    if not c: return
    try:
        for key in c.scan_iter(f"contextos:u{owner_id}:*"): c.delete(key)
    except Exception: pass

def invalidate_message_caches():  # backwards-compatible: drop everything
    c=client()
    if not c: return
    try:
        for key in c.scan_iter("contextos:u*"): c.delete(key)
    except Exception: pass
