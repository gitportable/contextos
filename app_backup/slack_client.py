"""Token-aware Slack Web API client (synchronous; used by FastAPI `def` routes and the worker).

The caller decides which token to pass. Tokens are never read from request data.
Handles: 429/Retry-After, transient 5xx/network retries, pagination, typed errors, and the
"did my chat.postMessage actually go through?" ambiguity for non-idempotent calls.
"""
import logging
import time
from typing import Any, Iterator

import httpx

from app.config import settings

log = logging.getLogger("contextos.slack")
SLACK_API_BASE = "https://slack.com/api"
_sleep = time.sleep  # patched in tests

AUTH_ERRORS = {"invalid_auth", "token_revoked", "token_expired", "account_inactive", "not_authed", "invalid_token", "no_permission_for_token"}
CHANNEL_ERRORS = {"channel_not_found", "not_in_channel", "is_archived", "thread_not_found", "missing_channel", "access_denied"}
TRANSIENT_ERRORS = {"internal_error", "fatal_error", "service_unavailable", "request_timeout", "ratelimited"}


class SlackApiError(Exception):
    def __init__(self, method: str, code: str, detail: dict | None = None):
        super().__init__(f"Slack {method}: {code}")
        self.method, self.code, self.detail = method, code, detail or {}


class SlackAuthError(SlackApiError):
    """Token revoked/expired/invalid: the user must reconnect."""


class SlackScopeError(SlackApiError):
    """The token lacks a required scope (`needed` / `provided` are Slack's own hints)."""
    @property
    def needed(self) -> str:
        return self.detail.get("needed", "")


class SlackChannelError(SlackApiError):
    """Conversation-specific problem (not a member, archived, gone). Skip that conversation, keep syncing others."""


class SlackRateLimited(SlackApiError):
    def __init__(self, method: str, retry_after: int):
        super().__init__(method, "ratelimited")
        self.retry_after = max(1, int(retry_after))


class SlackTransportError(SlackApiError):
    """Network / 5xx failure after retries. Safe to retry later for idempotent calls."""
    def __init__(self, method: str, message: str):
        super().__init__(method, "transport_error", {"message": message})


class SlackAmbiguousError(SlackApiError):
    """A non-idempotent call timed out after it may have reached Slack. Do NOT blindly retry."""
    def __init__(self, method: str, message: str):
        super().__init__(method, "ambiguous_outcome", {"message": message})


def _classify(method: str, code: str, data: dict) -> SlackApiError:
    if code in AUTH_ERRORS:
        return SlackAuthError(method, code, data)
    if code == "missing_scope":
        return SlackScopeError(method, code, data)
    if code in CHANNEL_ERRORS:
        return SlackChannelError(method, code, data)
    if code in TRANSIENT_ERRORS:
        return SlackTransportError(method, code)
    return SlackApiError(method, code, data)


class SlackClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None, timeout: float = 30.0,
                 max_inline_wait: int | None = None, retries: int = 3):
        self._http = httpx.Client(timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {token}"})
        self.max_inline_wait = settings.slack_max_inline_wait_seconds if max_inline_wait is None else max_inline_wait
        self.retries = retries

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SlackClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, method: str, params: dict[str, Any] | None = None, *, json_body: dict | None = None,
             idempotent: bool = True) -> dict:
        url = f"{SLACK_API_BASE}/{method}"
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._http.post(url, json=json_body) if json_body is not None else self._http.post(url, data=params or {})
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # request never left the machine
                if attempt <= self.retries:
                    _sleep(min(8, 2 ** attempt)); continue
                raise SlackTransportError(method, f"{type(exc).__name__}") from exc
            except httpx.HTTPError as exc:  # read timeout, reset: Slack may have processed it
                if not idempotent:
                    raise SlackAmbiguousError(method, f"{type(exc).__name__}") from exc
                if attempt <= self.retries:
                    _sleep(min(8, 2 ** attempt)); continue
                raise SlackTransportError(method, f"{type(exc).__name__}") from exc

            if resp.status_code == 429:
                wait = int(float(resp.headers.get("Retry-After", "30")))
                if wait <= self.max_inline_wait and attempt <= self.retries:
                    log.info("slack_rate_limited method=%s wait=%ss (inline)", method, wait)
                    _sleep(wait); continue
                raise SlackRateLimited(method, wait)
            if resp.status_code >= 500:
                if not idempotent:
                    raise SlackAmbiguousError(method, f"HTTP {resp.status_code}")
                if attempt <= self.retries:
                    _sleep(min(8, 2 ** attempt)); continue
                raise SlackTransportError(method, f"HTTP {resp.status_code}")
            if resp.status_code >= 400:
                raise SlackApiError(method, f"http_{resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise SlackTransportError(method, "non-JSON response") from exc
            if data.get("ok"):
                return data
            code = data.get("error", "unknown_error")
            if code == "ratelimited":
                raise SlackRateLimited(method, int(float(resp.headers.get("Retry-After", "30"))))
            raise _classify(method, code, data)

    def paginate(self, method: str, params: dict[str, Any], *, max_pages: int = 200) -> Iterator[dict]:
        """Follow response_metadata.next_cursor until exhausted. Yields whole pages."""
        cursor = None
        for _ in range(max_pages):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            data = self.call(method, page_params)
            yield data
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return
