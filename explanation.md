# ContextOS – Slack data pipeline (v0.4)

ContextOS turns each person's Slack into a **priority inbox**, **reminders**, **meeting suggestions** and a
**source-grounded assistant**. Every user connects their own Slack account through OAuth; everything stored and
shown is scoped to that user.

> **Verification status (read first).** The sandbox this was built in had no network, so FastAPI/SQLAlchemy/pytest
> could not be installed. What *was* executed: byte-compilation of every file, an undefined-name lint, the JS syntax
> check of `index.html`, and the 8 pure-logic tests in `tests/test_pure.py` (text normalisation, priority, date
> extraction in IST, token encryption). What was **not** executed: the database-backed tests (`test_pipeline.py`,
> `test_sync.py`), the API routes, the worker against PostgreSQL, and any real Slack call. Run `pytest` first
> (Step 6 below) and expect to fix small things.

---

## 1. Architecture

```
                     ┌──────────────── Browser (index.html, per-user session + CSRF) ───────────────┐
                     │  Connect Slack · Sync now · Disconnect · Priority inbox · Reply composer     │
                     └──────────────────────────────────┬───────────────────────────────────────────┘
                                                        │
 Slack ──Events API──► POST /integrations/slack/events ─┤  FastAPI (app/main.py, routes_slack.py)
 (signed)               verify signature                │  OAuth: /connect → Slack → /callback (state + code exchange)
                        receipt + job in ONE txn        │
                                                        ▼
                                          PostgreSQL = system of record + durable queue (processing_jobs)
                                                        │  FOR UPDATE SKIP LOCKED
                                                        ▼
                         Worker (python -m app.worker) ─ slack_event ─► fan-out to each member connection
                                                       ├ slack_process_message ─► pipeline.ingest_message
                                                       ├ slack_initial_sync / slack_incremental_sync
                                                       └ slack_reconcile_conversation
                                                        │
        history sync (slack_sync.py) ───────────────────┘   ONE ingestion path (app/pipeline.py):
        users.conversations → conversations.history          normalise → dedupe → score → reminder /
        → conversations.replies (threads)                    meeting extraction → commit → index
                                                        │
                                  Redis (optional cache) · Qdrant (vectors) · Neo4j (graph)  — all best-effort
```

Key idea: **live events, history sync, reconcile and the demo all call `pipeline.ingest_message()`**, so a message
is scored and de-duplicated exactly one way regardless of how it arrived.

## 2. Senior-lead review: what was wrong, and what changed

| # | Finding in the original pipeline | Impact | Change |
|---|---|---|---|
| 1 | No users, no tenancy: every endpoint returned every message/reminder/action; `/assistant/ask` and Qdrant searched all data | Anyone reaching your ngrok URL could read all Slack data | Accounts (scrypt), server-side sessions, CSRF token, `owner_id` on every table, user-scoped endpoints, per-user cache keys, Qdrant `owner_id` filter, MCP server bound to one user |
| 2 | Global `SLACK_BOT_TOKEN` and an admin-key history flow | One token for everyone; admin key typed into a web page | Per-user OAuth; tokens Fernet-encrypted; token chosen server-side per connection; admin-key route now returns 410 |
| 3 | Edits, deletes, thread replies, file shares, bot alerts all dropped (`if subtype: ignore`) | Missing/stale data (e.g. an edited deadline never updated) | Supported subtypes ingested; `message_changed` updates and re-derives reminders; `message_deleted` tombstones and cancels reminders; threads fetched via `conversations.replies` |
| 4 | History sync: one page, no pagination, no checkpoint, dedupe by `LIKE '%…%'` on job JSON | Silent truncation; slow, racy dedupe | Cursor pagination, resumable `latest`-boundary backfill, per-conversation checkpoints, DB unique constraints (`connection_id, channel_id, slack_ts`) |
| 5 | No rate-limit handling | Sync dies on HTTP 429 | `Retry-After` honoured: short waits inline, long waits **reschedule the job without consuming an attempt**; progress is committed per page |
| 6 | Jobs stuck in `processing` after a crash stayed forever; no periodic sync | Data silently stops flowing | Stuck-job recovery, heartbeat on long syncs, scheduler (incremental every 5 min, reconcile every 6 h), failed-job retention cleanup |
| 7 | Qdrant model reloaded for every message; new Qdrant/Neo4j client per message | Very slow ingestion | Process-wide model/client singletons; batch indexing per page; `indexed_at` so re-index only does what's missing |
| 8 | `.env` had `QDRANT_URL=https://` and `NEO4J_URI=neo4j+s://` (truthy placeholders) | Every message attempted and failed to connect | Treated as *unset* unless a real host is present |
| 9 | `APP_TIMEZONE` was never used; reminders at 09:00/17:00 **UTC** | Wrong reminder times for India | Dates computed in `APP_TIMEZONE`; also understands "by 5pm", "tomorrow at 3pm", "by Friday"; `tzdata` added for Windows |
| 10 | Slack mrkdwn scored raw (`<@U1>`, `<https://…|x>`, `&amp;`) | Poor scoring/search/embeddings | `slack_text.normalize_text`; author/mention names resolved via `users.info` cache |
| 11 | Priority ignored context | A DM or @-mention scored like chatter | Mention, DM, @channel boosts; negation ("not urgent"); bot-message handling; your own messages stored but kept out of the inbox and never create reminders |
| 12 | Reply: global token, no ownership check, retry could double-send | Duplicate or cross-user sends | Ownership + scope checks, explicit approval, idempotency key, audit row, **ambiguous timeout recorded as `unknown`** and later resolved by reconcile |
| 13 | Stale history created already-overdue reminders; guessed meeting times looked real | Noise / false precision | Reminders older than 24 h overdue are skipped; guessed meeting slots are flagged `is_provisional` and labelled in the UI |
| 14 | `create_all` can't upgrade tables | Schema drift | Alembic scaffold; startup refuses a legacy schema with a clear message |

## 3. What to keep from your original code
Signature verification (`slack_signing.py`), the durable PostgreSQL queue with `SKIP LOCKED`, `EventReceipt`
idempotency, the audit-first `OutboundAction`, fail-soft optional integrations, approval-gated replies and calendar
creation. These were good; they were extended, not replaced.

## 4. Data model (new/changed)
`users`, `user_sessions`, `oauth_states` · `google_calendar_connections` (owner-scoped, encrypted read-only Calendar tokens) ·
`slack_connections` (encrypted tokens, scopes, status, sync progress) ·
`slack_conversations` (membership + checkpoints: `backfill_cursor_ts`, `backfill_top_ts`, `high_watermark_ts`) ·
`slack_users` (name cache) · `slack_messages` (now per connection, with `thread_ts`, `is_deleted`, `edited_at`,
`mentions_me`, `is_self`, `indexed_at`) · `reminders`/`suggested_events` (owner + `auto_key` unique, one auto item per
message) · `processing_jobs` (+`connection_id`, `team_id`, unique `dedupe_key` while queued/processing) ·
`outbound_actions` (+idempotency key, `send_as`, statuses `sending|sent|failed|unknown|confirmed|unconfirmed|simulated`).

Messages are stored **per connection**, because priority is personal (your @-mentions, your DMs). The same Slack
message appears once for each connected user who is a member of that conversation.

## 5. How each feature works

**Connect Slack.** `/integrations/slack/connect` (login required) creates a single-use, 10-minute `state` bound to the
user and redirects to Slack with `user_scope=…`. `/callback` validates state + user, exchanges the code at
`oauth.v2.access`, encrypts tokens, stores workspace/user ID and granted scopes, then queues the first sync.
Reconnecting updates the same row.

**Historical sync.** `discover_conversations` (`users.conversations`, cursor-paginated, only types whose read **and**
history scopes were granted) → per conversation: backfill newest→oldest over `SLACK_INITIAL_SYNC_DAYS`, each page +
checkpoint committed in one transaction, thread replies fetched for parents. Later runs fetch only
`ts > high_watermark`. A crash or 429 resumes from the checkpoint.

**Live events.** Verified, de-duplicated by `event_id`, stored with a job in one transaction (so a crash after the 200
cannot lose an event). The worker fans out to connections that are members of the channel, one
`slack_process_message` job each. Edits and deletes follow the same route.

**Reconcile (every 6 h and after every reply).** Re-reads the last 3 days: applies edits, fetches missed thread
replies, and tombstones messages Slack no longer returns (self-heals a missed delete event). Also resolves `unknown`
reply actions by looking for the posted text.

**Priority & extraction.** Deterministic scoring with context boosts; reminders (`today/EOD`, `tomorrow`, `by 5pm`,
`by Friday`) in your timezone; meeting suggestions with explicit vs provisional times.

**Reply.** Login → ownership → active connection → `chat:write` scope → explicit approval → idempotency key → audit row
→ `chat.postMessage` (never auto-retried) → status `sent`, or `unknown` on timeout.

**Disconnect.** Revokes the *user* token (never the shared bot token), wipes stored tokens, cancels queued jobs,
optionally purges imported messages and index entries. Slack's `tokens_revoked` / `app_uninstalled` events do the
same automatically; a revoked token during sync marks the connection `needs_reauth` and shows a Reconnect button.

## 6. Setup on Windows with ngrok (step by step)

1. **Back up, then install**
   ```powershell
   cd "$env:USERPROFILE\Downloads\contextos-slack-mvp"
   Copy-Item .\app .\app-backup -Recurse; Copy-Item .\.env .\.env-backup
   # unzip this project over it, then:
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
   # optional: -r requirements-integrations.txt  (Qdrant, Neo4j, LangGraph, MCP, Google)
   ```
2. **Start ngrok** (a reserved/static domain avoids re-editing Slack settings): `ngrok http 8000` → copy the
   `https://….ngrok-free.app` URL.
3. **Edit `.env`** (start from `.env.example`): `PUBLIC_BASE_URL`, `SLACK_SIGNING_SECRET`, `SLACK_CLIENT_ID`,
   `SLACK_CLIENT_SECRET`, and
   `python -m app.crypto` → paste into `TOKEN_ENCRYPTION_KEY`. Empty `QDRANT_URL` / `NEO4J_URI` if you don't use them.
4. **Slack app settings** (api.slack.com/apps → your app)
   - *OAuth & Permissions → Redirect URLs:* `https://<ngrok>/integrations/slack/callback` (Slack requires HTTPS; the
     old `http://localhost` URL will not work with this flow).
   - *User Token Scopes:* the ones in `SLACK_USER_SCOPES` (`channels:history, channels:read, groups:history,
     groups:read, im:history, im:read, mpim:history, mpim:read, chat:write, users:read`). Remove any you don't want;
     the app only syncs conversation types whose scopes were granted.
   - *Event Subscriptions:* Request URL `https://<ngrok>/integrations/slack/events`; under **"Subscribe to events on
     behalf of users"** add `message.channels`, `message.groups`, `message.im`, `message.mpim`. Also add
     `tokens_revoked` and `app_uninstalled` if your app settings offer them (they may require a bot user; verify in
     your app's settings).
5. **Reset the old dev database once** (old rows have no owner and can't be migrated):
   `.\.venv\Scripts\python.exe scripts\reset_dev_db.py --yes`
   (or `alembic upgrade head` on a fresh database).
6. **Run** (three terminals): `docker compose up -d` · `python -m uvicorn app.main:app --reload` ·
   `python -m app.worker`. Tests: `python -m pytest`.
7. **Use it:** open **the ngrok URL** (not localhost – cookies and OAuth state must be on the same origin) →
   *Create account* → *Connect Slack* → approve → watch the Slack panel (sync progress, per-type counts) and the queue.

### Demo data (shows the whole pipeline without Slack)
Click **Load demo data** (or `python -m app.demo --email you@example.com`). It creates a demo workspace and feeds
Slack-shaped events through the real `accept_event → slack_event job → fan-out → slack_process_message → pipeline`
path: a P0 incident with thread replies, a bot alert, requests with deadlines, a DM meeting request, a provisional
meeting, an @channel broadcast, an FYI, a one-word chat, **an edit** (invoice deadline "tomorrow" → "Friday": the reminder
moves), **a delete** (reminder cancelled), your own message (kept out of the inbox) and **a replayed event** (dropped by
the receipt table). Replies on demo messages are recorded as `simulated`. *Remove demo data* deletes everything.

## 7. Acceptance checklist (run on your machine)
- [ ] `pytest` passes (DB tests use SQLite; some SQLAlchemy savepoint behaviour differs from PostgreSQL)
- [ ] Demo: 12 inbox messages, 1 P0 (the incident), reminders include "invoice … Friday", the deleted message is gone, replay dropped
- [ ] Register two accounts → each sees only its own data (try `/messages/<other id>` → 404)
- [ ] Connect Slack → DB shows `team_id`, `slack_user_id`, scopes; `user_token_encrypted` starts with `gAAAA`
- [ ] Initial sync: Slack panel shows conversations by type; re-click *Sync now* → no duplicate rows
- [ ] Post in a joined channel → appears within seconds (event path); edit it → text updates; delete it → disappears
- [ ] Reply → status `sent`; kill the network mid-send → `unknown`, resolved after reconcile
- [ ] Disconnect → no more syncs/sends; Slack app *Revoke* → connection shows `needs_reauth`

## 8. Known limits and things you must decide
- **Slack rate limits.** For *commercially distributed, non-Marketplace* apps, `conversations.history` and
  `.replies` are limited to **1 request/minute and 15 messages per request**; internal/custom apps keep the normal
  limits. If you are throttled, set `SLACK_HISTORY_PAGE_SIZE=15`: the sync becomes slow but stays correct and resumable.
- **Threads:** replies to threads whose parent is older than the reconcile window (3 days) arrive via live events only.
- **Out-of-order delete before create** can briefly resurrect a message; the next reconcile removes it.
- **Public channels you haven't joined** are not synced (the user token only sees what you're a member of).
- **Google Calendar schedule lookup** uses per-user OAuth and encrypted tokens. Legacy event creation still uses the deployment-level calendar credentials and is separate from this read-only connection.
- **Login** has basic throttling only; add email verification / password reset / 2FA before real multi-user use.
  Close registration (`ALLOW_REGISTRATION=false`) once your accounts exist, since ngrok URLs are public.
- Your original `requirements.txt` and `docker-compose.yml` were not uploaded; the `requirements.txt` here is a
  replacement, so merge by hand. `docker-compose.yml` is unchanged and not included.
- The old `SLACK_BOT_TOKEN` and `CONTEXTOS_ADMIN_KEY` settings are kept only so old `.env` files load; nothing uses them.

## 9. File map
`app/main.py` API · `routes_slack.py` OAuth/connection routes · `auth.py` sessions/CSRF · `slack_oauth.py` OAuth +
token lifecycle · `crypto.py` token encryption · `slack_client.py` token-aware API client · `slack_sync.py` history /
reconcile / queueing · `slack_events.py` event acceptance + fan-out · `pipeline.py` single ingestion path ·
`priority.py` scoring/dates · `slack_text.py` mrkdwn/ts helpers · `worker.py` jobs, recovery, scheduler ·
`integrations.py` Qdrant/Neo4j/LangGraph/Calendar · `demo.py` demo seeding · `mcp_server.py` per-user MCP ·
`migrations/` Alembic · `scripts/reset_dev_db.py` · `tests/`.
