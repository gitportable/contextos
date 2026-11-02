"""Sync against a fake Slack served through httpx.MockTransport (no network)."""
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from app import slack_sync
from app.jobs import RescheduleJob
from app.models import SlackConversation, SlackMessage
from app.slack_client import SlackClient


class FakeSlack:
    def __init__(self, n=5, thread_parent=None, rate_limit_on_call=None):
        base = time.time() - 3600
        self.msgs = [{"ts": f"{base + i:.6f}", "user": "UO", "text": f"message {i} please review"} for i in range(n)]
        if thread_parent is not None:
            self.msgs[thread_parent].update(reply_count=1, thread_ts=self.msgs[thread_parent]["ts"])
        self.calls, self.rate_limit_on_call, self.history_calls = [], rate_limit_on_call, 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        p = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(method)
        if method == "users.conversations":
            return httpx.Response(200, json={"ok": True, "channels": [{"id": "C1", "name": "eng", "is_private": False}], "response_metadata": {"next_cursor": ""}})
        if method == "users.info":
            return httpx.Response(200, json={"ok": True, "user": {"id": p["user"], "profile": {"display_name": "Pat"}}})
        if method == "conversations.history":
            self.history_calls += 1
            if self.rate_limit_on_call == self.history_calls:
                return httpx.Response(429, headers={"Retry-After": "120"}, json={"ok": False, "error": "ratelimited"})
            lo, hi, size = float(p.get("oldest", 0)), float(p.get("latest", 9e12)), int(p["limit"])
            rows = sorted([m for m in self.msgs if lo < float(m["ts"]) < hi], key=lambda m: float(m["ts"]), reverse=True)
            return httpx.Response(200, json={"ok": True, "messages": rows[:size], "has_more": len(rows) > size})
        if method == "conversations.replies":
            parent = next(m for m in self.msgs if m["ts"] == p["ts"])
            reply = {"ts": f"{float(p['ts']) + 0.5:.6f}", "user": "UO2", "text": "a reply", "thread_ts": p["ts"]}
            return httpx.Response(200, json={"ok": True, "messages": [parent, reply], "has_more": False})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})


def factory(fake):
    return lambda token: SlackClient(token, transport=httpx.MockTransport(fake.handler), max_inline_wait=0)


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    monkeypatch.setattr("app.config.settings.slack_history_page_size", 2)


def test_pagination_threads_and_idempotent_resync(db, conn):
    fake = FakeSlack(n=5, thread_parent=1)
    slack_sync.sync_connection(db, conn, "initial", client_factory=factory(fake))
    assert fake.history_calls == 3                                  # 5 messages / pages of 2
    assert db.query(SlackMessage).count() == 6                      # 5 + 1 thread reply
    conv = db.query(SlackConversation).one()
    assert conv.backfill_complete and conv.high_watermark_ts == max(m["ts"] for m in fake.msgs)
    slack_sync.sync_connection(db, conn, "incremental", client_factory=factory(fake))
    assert db.query(SlackMessage).count() == 6                      # no duplicates on re-sync


def test_rate_limit_checkpoints_and_resumes(db, conn):
    fake = FakeSlack(n=5, rate_limit_on_call=2)
    with pytest.raises(RescheduleJob) as exc:
        slack_sync.sync_connection(db, conn, "initial", client_factory=factory(fake))
    assert exc.value.delay_seconds == 120
    conv = db.query(SlackConversation).one()
    assert not conv.backfill_complete and conv.backfill_cursor_ts      # page 1 is saved
    assert db.query(SlackMessage).count() == 2
    slack_sync.sync_connection(db, conn, "initial", client_factory=factory(fake))   # resume
    assert db.query(SlackMessage).count() == 5 and db.query(SlackConversation).one().backfill_complete


def test_reconcile_detects_deletion(db, conn):
    fake = FakeSlack(n=3)
    slack_sync.sync_connection(db, conn, "initial", client_factory=factory(fake))
    fake.msgs.pop(1)                                                # user deleted a message in Slack
    slack_sync.sync_connection(db, conn, "reconcile", client_factory=factory(fake))
    assert db.query(SlackMessage).filter_by(is_deleted=True).count() == 1


def test_revoked_token_marks_connection(db, conn):
    from app.jobs import PermanentJobError
    handler = lambda req: httpx.Response(200, json={"ok": False, "error": "token_revoked"})  # noqa: E731
    with pytest.raises(PermanentJobError):
        slack_sync.sync_connection(db, conn, "initial", client_factory=lambda t: SlackClient(t, transport=httpx.MockTransport(handler)))
    db.refresh(conn)
    assert conn.status == "needs_reauth"
