# ContextOS architecture

This document describes the current implementation in `app/`, rather than a target design. The app is a FastAPI web/API process plus a separate worker process. The configured relational database (PostgreSQL in a normal deployment) stores accounts, Slack connections, messages, product data, pipeline traces, and durable jobs. Redis, Qdrant, and Neo4j are optional, derived/supporting stores—not replacements for the relational source of truth. See [database.md](./database.md) for the detailed storage inventory.

## 1. Runtime components

```text
Browser (app/static/index.html, app.js, app.css)
    │ HTTP + session cookie; CSRF header on state-changing calls
    ▼
FastAPI (app/main.py)
    ├── auth/account/messages/reminders/schedule/queue endpoints
    ├── app/routes_slack.py: per-user Slack OAuth and connection endpoints
    ├── app/routes_app.py: Today, projects, assistant, admin pipeline endpoints
    ├── app/security.py: Host, Origin, body-size and response-header controls
    └── Relational database (app/database.py, app/models.py; PostgreSQL in deployment)
          ├── user-owned product records and encrypted OAuth tokens
          ├── processing_jobs: durable queue and sync work
          └── pipeline_events / worker_heartbeats: operational visibility
                ▲
                │ claim jobs, persist progress/results
Worker (python -m app.worker)
    ├── Slack event processing and history/reconcile jobs
    ├── message ingestion, scoring, extraction and project discovery
    ├── summaries, retention and periodic maintenance
    └── optional indexing / external integrations

Slack ── OAuth redirect to /integrations/slack/callback
Slack ── signed Events API POST to /integrations/slack/events

Optional services: Redis · Qdrant · Neo4j · Groq/Gemini · Google Calendar (credentials/token files)
```

## 2. Source code ownership

| Module | Responsibility |
|---|---|
| `app/main.py` | FastAPI setup, auth/account, Slack Events API intake, messages/replies, queue metrics, reminders, schedule, demo and reindex endpoints |
| `app/routes_slack.py` | Slack OAuth start/callback, current user's connection listing, directory, sync, disconnect |
| `app/routes_app.py` | Today/briefing, message actions, assistant, projects, admin pipeline and settings |
| `app/auth.py`, `app/google_auth.py` | Session authentication, password handling, owner role, Google sign-in |
| `app/security.py`, `app/ratelimit.py`, `app/validators.py` | HTTP security middleware, request validation, limits |
| `app/models.py`, `app/database.py`, `migrations/` | SQLAlchemy models, DB sessions, schema migrations |
| `app/slack_oauth.py`, `app/slack_client.py`, `app/slack_signing.py` | Slack OAuth/token lifecycle, Slack Web API requests, Events API verification |
| `app/slack_events.py`, `app/slack_sync.py`, `app/worker.py` | Durable event/job creation, resumable sync, job execution and periodic work |
| `app/pipeline.py`, `app/priority.py`, `app/projects.py`, `app/summaries.py` | Canonical message ingestion, scoring/extraction, project discovery, retrieval and generated summaries/answers |
| `app/integrations.py`, `app/cache.py`, `app/trace.py` | Optional external services, caching and pipeline-event recording |
| `app/static/` | Browser-based single-page interface |

## 3. Request and data flows

### 3.1 Authentication and per-user access

Password or Google authentication establishes a server-side `user_sessions` row. The raw random session token is sent in an HTTP-only cookie; the database stores its hash. Authenticated requests resolve the current user from that cookie. Mutating requests also need the session CSRF token in `X-CSRF-Token`.

`ADMIN_EMAIL` assigns the deployment-owner role to a ContextOS user. Admin-only endpoints are guarded separately from regular per-user data APIs. Connecting Slack does not affect the role.

Slack OAuth state identifies the ContextOS user who initiated the flow. On callback, the code saves the Slack connection under that user's `owner_id`, keyed by `(owner_id, team_id)`. The connection-list endpoint filters by that owner. The Slack consent screen still uses the Slack account/workspace available in the browser; ContextOS does not select a workspace by email.

### 3.2 Slack OAuth and authorization

```text
Signed-in browser
  GET /integrations/slack/connect?preset=...&reply=...
    → create single-use, expiring OAuth state bound to ContextOS user
    → redirect to Slack OAuth v2
  Slack consent
  GET /integrations/slack/callback?code=...&state=...
    → check session and consume state
    → exchange code with Slack
    → encrypt/save user and optional bot tokens, scopes, Slack identity
    → enqueue initial sync and redirect back to the app
```

The server derives requested scopes from a preset. Defaults are read-only user scopes; private conversation scopes and `chat:write` are optional. Users must still be allowed by Slack app distribution and their workspace's access policy.

### 3.3 History sync and events

- OAuth completion queues an initial sync; account settings can queue another sync.
- Sync code pages through accessible conversations and history, persisting job/checkpoint progress.
- `/integrations/slack/events` verifies the Slack signature and request timestamp. Event IDs are recorded for deduplication, and processing work is queued before returning a successful response.
- Worker jobs use the connection, workspace, and owner context to process the data. Event fan-out and message handling are implemented in `app/slack_events.py`, `app/slack_sync.py`, and `app/worker.py`.

The API process handles the request and enqueues work; the worker performs long-running sync and processing. Run both processes for live data.

### 3.4 Message processing

History sync, events, reconciliation, and development demo data feed into the shared ingestion pipeline. In broad strokes, it:

1. Normalizes the Slack message and associates the owner/connection.
2. Deduplicates or updates the stored message.
3. Calculates priority and reason.
4. Extracts reminder and schedule suggestions where applicable.
5. Tags eligible messages with the user's projects. Automatic discovery requires recurring evidence and considers explicit project/channel names plus repeated structured title prefixes; users can also search stored messages by keyword and create a project manually from the Projects page. Manual creation tags matching owner messages immediately.
6. Commits canonical records and records pipeline events.
7. Attempts optional vector/graph indexing after persistence.

The relational database is the authoritative store. Optional indexers and LLM calls can fail independently; their status is surfaced through pipeline tools and do not replace the canonical message row. Qdrant and Neo4j contain derived copies and must be treated as rebuildable indexes.

Project matching uses case-insensitive keyword boundaries shared by preview, manual tagging, and live ingestion. If several active projects match, the strongest and most specific phrase wins; equally strong matches remain unassigned to avoid arbitrary project filters. Manual search only searches already-synced Slack messages and does not trigger a Slack history import.

Calendar-style Slack messages with an explicit date and time range are parsed into non-provisional suggestions. Today's events appear with today's reminders on the Today view, while linked event messages are discoverable in the inbox and Ask results. Repeated identical event suggestions are deduplicated in schedule read models.

### 3.5 Worker queue

`processing_jobs` is a relational-database-backed queue. The worker claims available jobs, dispatches by job type, persists completion/failure and retry scheduling, and periodically records a heartbeat. Job types include Slack event handling, history sync/reconciliation, project discovery, summaries, and retention. Queue and heartbeat details are implementation-specific; consult `app/worker.py` and `app/jobs.py` for the exact behavior.

### 3.6 Optional integrations

| Service | Current role | Core behavior without it |
|---|---|---|
| Redis | Cache and optional rate-limit backend | Code has fallback behavior; exact mode depends on configuration |
| Qdrant | Semantic message indexing/retrieval | SQL/keyword retrieval remains available |
| Neo4j | People/project/channel graph queries | Graph-backed views may report unavailable; PostgreSQL records remain |
| Groq/Gemini | LLM summaries, drafts, and answers | Non-LLM fallback behavior is available |
| Google Calendar | Optional approved-event creation | Suggestions can still be stored and approved without calendar creation |

Not all integrations are required or enabled by default. Their availability depends on `.env` and installed optional dependencies.

## 4. Persistence and ownership

Principal model groups include:

- Accounts and security: `users`, `user_sessions`, `oauth_states`, and `rate_limits`.
- Slack: `slack_connections`, `slack_conversations`, `slack_users`, `slack_messages`, and `event_receipts`.
- Product data: `projects`, `reminders`, `suggested_events`, `summaries`, `assistant_notes`, and `outbound_actions`.
- Operations: `processing_jobs`, `pipeline_events`, and `worker_heartbeats`.

User-generated records include an owner or are reachable through an owner-scoped Slack connection. API handlers must scope reads and writes to the authenticated user. Slack assignee IDs are checked against connected workspaces before assignment.

Redis holds only expendable cached responses and rate-limit counters. Qdrant stores semantic vectors and message payloads; Neo4j stores graph relationships used for people/project/channel views. Google Calendar OAuth credentials and tokens are filesystem files, not rows in the application database. The app may also keep project matchers in process memory for a short period.

Schema changes belong in `migrations/`. `init_db()` may create missing tables for development, but `create_all` does not upgrade existing tables. Apply Alembic migrations when upgrading an existing database. SQLite is used by automated tests and may be used for local development, but deployment guidance assumes PostgreSQL.

## 5. HTTP/API conventions and operational visibility

The browser calls same-origin JSON endpoints. Most authenticated endpoints use the session cookie; mutating endpoints require `X-CSRF-Token`. Errors generally return FastAPI's `detail` value, often with `code` and `message`. The full endpoint catalogue and examples are in [api.md](./api.md).

The user-facing pipeline metrics and owner-only admin pipeline views are distinct:

- `/api/pipeline/metrics` returns metrics scoped to the signed-in user.
- `/api/pipeline/overview`, `/api/pipeline/events`, and `/api/admin/settings` require the configured owner.
- `/api/pipeline/trace/{message_id}` allows access to the owner's message and permits admin-level cross-user inspection.

`/docs` and `/openapi.json` are enabled only when `APP_ENV=development`.

## 6. Reliability and security boundaries

- PostgreSQL stores canonical messages and durable jobs; a worker restart should not erase queued work.
- Slack Events API signatures and timestamps are verified before an event is accepted.
- OAuth state is single-use, time-limited, and linked to the initiating ContextOS user.
- Slack tokens are encrypted at rest; keep `TOKEN_ENCRYPTION_KEY` backed up and stable for existing tokens.
- Browser mutation requests are CSRF protected and checked for an allowed Origin. Responses include security headers and API no-store behavior.
- External service calls can fail or be rate-limited. Observe worker logs and pipeline status rather than assuming an HTTP 200 means all asynchronous work has completed.

This is not a guarantee of production readiness. In particular, validate deployment secrets, proxy trust, backups, account recovery, dependency maintenance, privacy retention, Slack scope/distribution configuration, and integration behavior in the actual deployment.

## 7. Validate changes

Install development dependencies and run `python -m pytest`. Use a dedicated test database and disable or isolate external services in test configuration. For frontend changes, run `node --check app/static/app.js`. Follow [README.md](./README.md) for local setup.
