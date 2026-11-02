"""One small client for any OpenAI-compatible chat endpoint (Groq by default; also Gemini's OpenAI-compatible URL,
Cerebras, OpenRouter...). Free tiers are rate limited, so every call passes three gates BEFORE it hits the network:

  1. per-minute limiter  (Redis; LLM_RPM, set a little under the provider's cap)
  2. global daily cap    (Redis; LLM_DAILY_CAP)
  3. per-user daily cap  (PostgreSQL llm_usage; LLM_USER_DAILY_CAP) so one user cannot burn everyone's quota

When a gate is closed or the provider answers 429, callers get LLMRateLimited(retry_after): background jobs reschedule
themselves without using a retry attempt; interactive calls fall back to extractive (non-LLM) output. Prompts, message
text and the API key are never logged.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import ratelimit
from app.config import settings
from app.models import LlmUsage

log = logging.getLogger("contextos.llm")
_sleep = time.sleep  # patched in tests


class LLMUnavailable(Exception):
    """Not configured, or the call failed after retries."""


class LLMRateLimited(LLMUnavailable):
    def __init__(self, retry_after: int, scope: str = "llm"):
        super().__init__(f"LLM rate limited ({scope}); retry in {retry_after}s")
        self.retry_after, self.scope = max(1, int(retry_after)), scope


def enabled() -> bool:
    return settings.llm_enabled


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _seconds_to_midnight_utc() -> int:
    now = datetime.now(timezone.utc)
    return max(60, 86400 - (now.hour * 3600 + now.minute * 60 + now.second))


def _reserve(db: Session, owner_id: int) -> int:
    """Take one slot from all three gates. Raises LLMRateLimited without calling out.

    Order matters: the cheap, user-specific gate goes FIRST so a user who is over their own cap can never burn the shared
    (global) quota with rejected attempts. The counter is bumped with an atomic UPDATE (two workers cannot lose an increment).
    Returns the llm_usage row id."""
    day = _today()
    usage = db.query(LlmUsage).filter_by(owner_id=owner_id, day=day).first()
    if usage is None:
        try:
            with db.begin_nested():
                usage = LlmUsage(owner_id=owner_id, day=day, calls=0, failures=0)
                db.add(usage)
                db.flush()
        except IntegrityError:
            usage = db.query(LlmUsage).filter_by(owner_id=owner_id, day=day).one()
    if usage.calls >= settings.llm_user_daily_cap:
        raise LLMRateLimited(_seconds_to_midnight_utc(), "user_daily")
    try:
        ratelimit.hit("llm:rpm", "all", settings.llm_rpm, 60)
        ratelimit.hit("llm:day", day, settings.llm_daily_cap, 86400)
    except ratelimit.RateLimited as exc:
        raise LLMRateLimited(exc.retry_after, exc.scope) from exc
    db.query(LlmUsage).filter(LlmUsage.id == usage.id).update({LlmUsage.calls: LlmUsage.calls + 1}, synchronize_session=False)
    db.commit()
    return usage.id


def _fail(db: Session, usage_id: int) -> None:
    db.query(LlmUsage).filter(LlmUsage.id == usage_id).update({LlmUsage.failures: LlmUsage.failures + 1}, synchronize_session=False)
    db.commit()


def _retry_after(resp: httpx.Response) -> int:
    try:
        return max(1, min(3600, int(float(resp.headers.get("retry-after", "30")))))
    except (TypeError, ValueError):
        return 30


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def chat(db: Session, owner_id: int, messages: list[dict], *, max_tokens: int = 700, json_mode: bool = False,
         transport: httpx.BaseTransport | None = None) -> str:
    if not enabled():
        raise LLMUnavailable("LLM_API_KEY is not configured")
    usage_id = _reserve(db, owner_id)
    body = {"model": settings.llm_model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    last = "unknown"
    for attempt in range(settings.llm_max_retries + 1):
        try:
            with httpx.Client(timeout=settings.llm_timeout_seconds, transport=transport) as client:
                resp = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            last = type(exc).__name__
            _sleep(min(8, 2 ** attempt))
            continue
        if resp.status_code == 429:
            _fail(db, usage_id)
            raise LLMRateLimited(_retry_after(resp), "provider")
        if resp.status_code == 400 and json_mode and "response_format" in body:
            body.pop("response_format")  # some models/providers reject JSON mode: retry plain, we parse defensively anyway
            continue
        if resp.status_code >= 500:
            last = f"HTTP {resp.status_code}"
            _sleep(min(8, 2 ** attempt))
            continue
        if resp.status_code != 200:
            _fail(db, usage_id)
            log.warning("llm_http_error status=%s", resp.status_code)  # body may echo the prompt: not logged
            raise LLMUnavailable(f"LLM provider returned HTTP {resp.status_code}")
        try:
            content = resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            _fail(db, usage_id)
            raise LLMUnavailable("LLM response had an unexpected shape") from exc
        return _THINK.sub("", content).strip()
    _fail(db, usage_id)
    raise LLMUnavailable(f"LLM call failed after retries ({last})")


def _emit_visible(token: str, state: dict, on_token) -> None:
    opening, closing = "<think>", "</think>"
    state["buffer"] += token

    def emit(text):
        if text:
            on_token(text)
            state["emitted"] = True

    while state["buffer"]:
        if state["thinking"]:
            end = state["buffer"].find(closing)
            if end >= 0:
                state["buffer"] = state["buffer"][end + len(closing):]
                state["thinking"] = False
                continue
            state["buffer"] = state["buffer"][-(len(closing) - 1):]
            return

        start = state["buffer"].find(opening)
        if start >= 0:
            emit(state["buffer"][:start])
            state["buffer"] = state["buffer"][start + len(opening):]
            state["thinking"] = True
            continue

        keep = 0
        for size in range(1, min(len(opening), len(state["buffer"]) + 1)):
            if state["buffer"].endswith(opening[:size]):
                keep = size
        if len(state["buffer"]) > keep:
            emit(state["buffer"][:-keep] if keep else state["buffer"])
            state["buffer"] = state["buffer"][-keep:] if keep else ""
        return


def chat_stream(db: Session, owner_id: int, messages: list[dict], on_token, *, max_tokens: int = 700,
                transport: httpx.BaseTransport | None = None) -> str:
    """Stream visible completion text as provider deltas arrive; return the cleaned full answer."""
    if not enabled():
        raise LLMUnavailable("LLM_API_KEY is not configured")
    usage_id = _reserve(db, owner_id)
    body = {"model": settings.llm_model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens, "stream": True}
    headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    last = "unknown"
    state = {"buffer": "", "thinking": False, "emitted": False}
    content = []
    with httpx.Client(timeout=settings.llm_timeout_seconds, transport=transport) as client:
        for attempt in range(settings.llm_max_retries + 1):
            try:
                with client.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code == 429:
                        _fail(db, usage_id)
                        raise LLMRateLimited(_retry_after(resp), "provider")
                    if resp.status_code >= 500:
                        last = f"HTTP {resp.status_code}"
                        if attempt < settings.llm_max_retries and not content:
                            _sleep(min(8, 2 ** attempt))
                            continue
                        break
                    if resp.status_code != 200:
                        _fail(db, usage_id)
                        log.warning("llm_http_error status=%s", resp.status_code)
                        raise LLMUnavailable(f"LLM provider returned HTTP {resp.status_code}")
                    for line in resp.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            delta = json.loads(data)["choices"][0]["delta"].get("content") or ""
                        except (KeyError, IndexError, ValueError, TypeError) as exc:
                            _fail(db, usage_id)
                            raise LLMUnavailable("LLM response had an unexpected shape") from exc
                        if delta:
                            content.append(delta)
                            _emit_visible(delta, state, on_token)
                    if state["buffer"] and not state["thinking"]:
                        on_token(state["buffer"])
                    answer = _THINK.sub("", "".join(content)).strip()
                    if state["thinking"]:
                        answer = answer.split("<think>", 1)[0].strip()
                    return answer
            except httpx.HTTPError as exc:
                last = type(exc).__name__
                if attempt >= settings.llm_max_retries or content:
                    break
                _sleep(min(8, 2 ** attempt))
        _fail(db, usage_id)
    raise LLMUnavailable(f"LLM stream failed after retries ({last})")


def parse_json_object(text: str) -> dict | None:
    """Models sometimes wrap JSON in prose or code fences. Pull out the first balanced {...} and parse it."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            depth += ch == "{"
            depth -= ch == "}"
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict):
                        return obj
                except ValueError:
                    pass
                break
        start = text.find("{", start + 1)
    return None


def fence(text: str) -> str:
    """Neutralise anything in untrusted message text that could close our data block or fake a role marker."""
    return (text or "").replace("<", "‹").replace(">", "›").replace("\x00", "")
