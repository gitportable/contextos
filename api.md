# ContextOS HTTP API

This guide describes the current FastAPI routes in `app/main.py`, `app/routes_slack.py`, and `app/routes_app.py`. It is a practical guide, not a substitute for request/response schemas in code. In development, OpenAPI is available at `/docs` and `/openapi.json`. For storage ownership and which database contains each record, see [database.md](./database.md).

## Base URL and request conventions

- Use the same origin as the browser app. In local development this is commonly `http://127.0.0.1:8000`; with Slack OAuth, use the configured public HTTPS host.
- JSON request bodies use `Content-Type: application/json`.
- Authenticated requests send the `contextos_session` cookie (or the configured session-cookie name).
- All state-changing requests from the browser must include `X-CSRF-Token`, obtained from `/auth/me` or the successful registration/login response. The cookie is HTTP-only; JavaScript sends the CSRF token header.
- The server checks the `Origin` header for unsafe methods. Slack's Events API endpoint is exempt from the browser Origin check but requires Slack signature validation.
- Request bodies are capped by `MAX_BODY_BYTES` (default 1 MB).
- Errors use standard HTTP status codes and typically a JSON body shaped like `{"detail":{"code":"...","message":"..."}}`. Some FastAPI validation errors use the framework's validation-detail array.
- `/docs` is enabled only for `APP_ENV=development`.

Example authenticated browser request:

```js
const session = await fetch("/auth/me").then(r => r.json());
const response = await fetch("/reminders", {
  method: "POST",
  credentials: "same-origin",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": session.csrf_token
  },
  body: JSON.stringify({ title: "Follow up", due_at: "2026-10-22T10:00:00+05:30" })
});
```

## How a browser request travels through the app

```text
app/static/app.js
  → same-origin HTTP request
  → security middleware (Host, Origin, body size, headers)
  → FastAPI route
  → authentication dependency (session + CSRF for mutations)
  → owner-scoped SQLAlchemy query or service call
  → relational database commit / response (PostgreSQL in deployment)
```

Long-running operations such as Slack history import are queued rather than completed within the browser request:

```text
POST /integrations/slack/sync
  → validate current user's connected Slack connection
  → insert/reuse processing job in the relational database
  → return queued status
  → app.worker claims the job and calls Slack
  → worker persists messages/progress; UI polls connection/pipeline status
```

### Which store an API operation uses

| API operation | Primary persistence |
|---|---|
| Authentication, account settings, sessions, Slack OAuth/connection state | Relational database |
| `/messages`, `/api/today`, `/reminders`, `/schedule/suggestions`, `/api/projects` | Owner-scoped relational database queries and writes |
| `/integrations/slack/sync` and `/integrations/slack/events` | Durable jobs/receipts in the relational database; worker writes canonical Slack records there |
| `/assistant/ask` and project summaries | Relational messages/summaries plus optional Qdrant retrieval and configured LLM provider |
| Project people/channel graph detail | Neo4j when available, otherwise relational SQL |
| `/api/pipeline/*` and `/queue/*` | Relational operational records; some counters/rate limits may use Redis |

PostgreSQL (or the configured relational database) remains authoritative. Qdrant and Neo4j are derived indexes; Redis caches and counters are not durable product records. See [database.md](./database.md) for the complete table and payload inventory.

## Authentication and account

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/config` | Public | Safe UI flags: registration, Google sign-in, demo |
| `POST` | `/auth/register` | Public | Create a password account; returns user and CSRF token and sets session cookie |
| `POST` | `/auth/login` | Public | Sign in; returns user and CSRF token and sets session cookie |
| `GET` | `/auth/google/start` | Public | Start Google OAuth; redirects to Google |
| `GET` | `/auth/google/callback` | OAuth callback | Complete Google sign-in and set session cookie |
| `GET` | `/auth/me` | Optional session | Return authentication status; authenticated response includes user and CSRF token |
| `POST` | `/auth/logout` | User + CSRF | Delete current session and clear cookie |
| `PATCH` | `/auth/account` | User + CSRF | Update display name |
| `POST` | `/auth/password` | User + CSRF | Change password for password-enabled accounts |
| `GET` | `/auth/sessions` | User | List active sessions for current user |
| `DELETE` | `/auth/sessions/{session_id}` | User + CSRF | Revoke another session belonging to current user |

The owner role is derived from `ADMIN_EMAIL`; there is no separate admin login. Password account registration can be disabled with `ALLOW_REGISTRATION=false`.

## Slack OAuth and connection management

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/integrations/slack/connect` | User session | Start OAuth; accepts `preset` (`essentials` or `everything`), `reply` and `keep` booleans |
| `GET` | `/integrations/slack/callback` | OAuth callback + initiating session | Validate state, exchange code, save the current user's connection, queue sync, redirect to app |
| `GET` | `/integrations/slack/connection` | User | List only the current user's Slack connections and their status, including disconnected connections |
| `GET` | `/integrations/slack/people?q=...` | User | Search directory entries from the user's connected workspaces; may refresh data from Slack |
| `POST` | `/integrations/slack/sync` | User + CSRF | Queue sync; optional JSON `{ "connection_id": 1, "mode": "reconcile" }`; modes: `auto`, `initial`, `incremental`, `reconcile` |
| `POST` | `/integrations/slack/disconnect` | User + CSRF | Disconnect an owned connection; body includes `connection_id` and optional `purge` |
| `POST` | `/integrations/slack/events` | Slack signature | Slack Events API endpoint; signature and timestamp must validate |

OAuth setup:

- Callback URL: `{PUBLIC_BASE_URL}/integrations/slack/callback`.
- Events API URL: `{PUBLIC_BASE_URL}/integrations/slack/events`.
- They are different endpoints and must be configured separately in Slack.
- OAuth authorization is for the Slack account/workspace selected in Slack's browser session, not a workspace looked up from the ContextOS email.

### Example: start a Slack connection

```http
GET /integrations/slack/connect?preset=essentials&reply=false HTTP/1.1
Host: app.example.test
Cookie: contextos_session=...
```

The response is a `302` redirect to Slack. After consent, Slack redirects to the callback. A successful callback queues a sync; the subsequent Slack connection response shows the saved workspace and sync progress.

## Messages and replies

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/messages` | User | List owned messages; filters include `limit`, `priority`, `include_self`, `channel_id`, `q`, `project_id` |
| `GET` | `/messages/{message_id}` | User | Read one owned message |
| `POST` | `/messages/{message_id}/reply` | User + CSRF | Send an explicitly approved reply using the user's connection; body includes reviewed text and `approved: true` |
| `GET` | `/integrations/slack/channels` | User | List active channels in the caller's connected Slack workspaces and whether the user can post |
| `POST` | `/channels/{connection_id}/{channel_id}/messages` | User + CSRF | Post reviewed text to an owned active Slack channel; requires `approved: true` and accepts `client_request_id` |
| `GET` | `/actions` | User | List this user's outbound action audit records |
| `POST` | `/messages/{message_id}/handled` | User + CSRF | Mark a message handled |
| `POST` | `/messages/{message_id}/snooze` | User + CSRF | Snooze for a specified number of hours (default 4; range 1–168) |
| `POST` | `/messages/{message_id}/suggest-reply` | User + CSRF | Generate a reply suggestion for an owned message; optional tone |

Both Slack send endpoints check that their message or channel and Slack connection belong to the caller, require explicit approval, and honor an optional `client_request_id` idempotency key. A timeout may have an ambiguous outcome; check the action record and Slack before retrying. Demo workspaces record simulated actions without sending to Slack.

## Workspace, Ledger, assistant, projects

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/today` | User | Today's priority messages, reminders, suggestions, and waiting items |
| `GET` | `/api/briefing?refresh=false` | User | Daily briefing; `refresh=true` requests regeneration and is rate-limited |
| `GET` | `/api/items/brief?limit=7` | User | Ranked, capped Brief from confirmed active items; reports `scoring: rules_only` |
| `GET` | `/api/items?state_filter=active&limit=100` | User | Owner-scoped Ledger items, ranked by transparent cold-start rules |
| `POST` | `/api/items` | User + CSRF | Create a confirmed item; accepts title, kind, direction, optional due date, and validated source message IDs |
| `PATCH` | `/api/items/{item_id}` | User + CSRF | Edit title/due date or change lifecycle state; requires `expected_version`, returns 409 on stale writes |
| `DELETE` | `/api/items/{item_id}` | User + CSRF | Delete an owned item while retaining its PII-light event history |
| `POST` | `/assistant/ask` | User + CSRF | Ask about the caller's own stored data; returns a concise answer and optional sources. Accepts up to 8 recent `{role, content}` turns for follow-up context. "Important today" queries only return recent priority messages that explicitly mention a deadline today. Person-directed message requests return a reviewable channel draft |
| `POST` | `/assistant/ask/stream` | User + CSRF | Same grounded answer over server-sent events (`token`, then `done`; `error` on failure) so the browser can display generated text as it arrives; accepts the same bounded chat history |
| `GET` | `/integrations/google-calendar/connection` | User | Return whether this user has a read-only Google Calendar connection |
| `GET` | `/integrations/google-calendar/events/today` | User | Return actual Calendar events for today in the configured timezone; no message-derived meeting guesses |
| `GET` | `/integrations/google-calendar/connect` | User | Start Google OAuth consent for read-only Calendar access |
| `GET` | `/integrations/google-calendar/callback` | OAuth callback | Finish the user-bound Calendar OAuth flow and store encrypted tokens |
| `POST` | `/integrations/google-calendar/disconnect` | User + CSRF | Revoke and remove this user's Calendar credentials |
| `GET` | `/api/projects` | User | List owned projects; supports `include_archived` |
| `GET` | `/api/projects/matches?keywords=...` | User | Preview owned, synced Slack messages matching comma-separated keywords |
| `POST` | `/api/projects` | User + CSRF | Create/reactivate a project and immediately tag matching messages; body: `{ "name": "Nebula launch", "keywords": "nebula, nebula launch" }` |
| `GET` | `/api/projects/{project_id}` | User | Project detail and related user-owned information |
| `POST` | `/api/projects/{project_id}/summarize` | User + CSRF | Generate/update the project's summary (rate-limited) |
| `PATCH` | `/api/projects/{project_id}` | User + CSRF | Update project properties such as name, keywords, or status |
| `POST` | `/api/projects/discover` | User + CSRF | Trigger project discovery for current user |

The browser uses `/assistant/ask/stream`; each `token` event contains a `text` delta, and the final `done` event contains the complete answer and citations. `/assistant/ask` remains available for non-streaming clients. Channel drafts are not sent automatically; the user selects a channel, reviews the text, and explicitly confirms before posting. The Workspace shows actual Google Calendar events; message text alone does not create meeting suggestions or commitments.

Project keyword previews and tagging use the same case-insensitive whole-term matching. When a message matches multiple active projects, the most specific matching keyword wins; an equal-strength tie is left untagged rather than assigned arbitrarily. Newly tagged messages can then be filtered with `GET /messages?project_id=...`. Search operates on messages already stored by Slack sync; it does not fetch new Slack history. Automatic discovery also considers repeated structured message-title prefixes (for example, a product/cohort name before a dash).

Manually created schedule suggestions remain available through the schedule API. The ingestion pipeline no longer creates guessed reminders or meeting suggestions from message text.

## Reminders and schedule suggestions

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/reminders` | User + CSRF | Create a reminder; optional message link and Slack assignee IDs |
| `GET` | `/reminders` | User | List reminders; supports `status`, `limit`, `person_id` |
| `POST` | `/reminders/{reminder_id}/complete` | User + CSRF | Complete an owned reminder |
| `PATCH` | `/reminders/{reminder_id}/assignees` | User + CSRF | Replace assignees with validated connected-workspace people |
| `GET` | `/schedule/suggestions` | User | List schedule suggestions, including source-message priority when linked; supports `status`, `person_id` |
| `POST` | `/schedule/suggestions` | User + CSRF | Create a suggestion; optional message link and Slack assignee IDs |
| `POST` | `/schedule/suggestions/{event_id}/approve` | User + CSRF | Record approval; does not itself create a calendar event |
| `PATCH` | `/schedule/suggestions/{event_id}/assignees` | User + CSRF | Replace assignees |
| `POST` | `/schedule/suggestions/{event_id}/approve-and-create` | User + CSRF | Attempt calendar creation using configured Google Calendar credentials |

Assignee values are IDs in the form `team_id:slack_user_id`. The server validates that each person belongs to a connected Slack workspace owned by the current user.

Example reminder body:

```json
{
  "title": "Follow up with the team",
  "due_at": "2026-10-22T10:00:00+05:30",
  "message_id": 123,
  "assignees": ["T123:U456"]
}
```

## Queue, metrics, and administration

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/queue/summary` | User | Queue counts for the caller's Slack work |
| `GET` | `/queue/jobs` | User | List caller-visible queue jobs; supports `status`, `limit` |
| `GET` | `/api/pipeline/metrics` | User | Personal metrics |
| `GET` | `/dashboard/summary` | User | Compact personal dashboard counts |
| `GET` | `/api/pipeline/overview` | Owner admin | System-wide pipeline status and counters |
| `GET` | `/api/pipeline/events` | Owner admin | Pipeline event feed; supports `after_id`, `limit`, `stage` |
| `GET` | `/api/pipeline/trace/{message_id}` | Owner or message owner | Trace of an accessible message |
| `GET` | `/api/admin/settings` | Owner admin | Safe system configuration status |
| `GET` | `/api/integrations` | Owner admin | Status of configured integrations |
| `POST` | `/integrations/qdrant/reindex` | User + CSRF | Re-index eligible messages for the caller |

The admin role is based on `ADMIN_EMAIL`, not Slack membership or a special password. Non-admin users cannot read the system-wide pipeline overview/events or settings.

## Development and operations

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | Public | Serve browser app |
| `GET` | `/static/{name}` | Public | Serve approved static assets |
| `GET` | `/health` | Public | Check database connectivity |
| `POST` | `/api/demo/seed` | User + CSRF | Add development demo data when demo mode is enabled |
| `POST` | `/api/demo/reset` | User + CSRF | Reset the caller's demo data when demo mode is enabled |

`POST /integrations/slack/sync-history` is a deprecated hidden route and returns HTTP `410 Gone`. Use per-user Slack connect and sync endpoints instead.

## Common status codes

| Status | Meaning |
|---|---|
| `400` | Invalid input or request |
| `401` | Missing/invalid session or invalid login |
| `403` | CSRF/origin/admin restriction or permission denied |
| `404` | Resource does not exist for this user |
| `409` | Conflict such as existing account, inactive Slack connection, missing grant, or duplicate action |
| `413` | Request body exceeds configured limit |
| `422` | Request body does not match its Pydantic schema |
| `429` | Rate limit or Slack/provider throttling |
| `503` | Required integration is unavailable or not configured |

For the exact accepted JSON fields and response fields, see `app/schemas.py` and the route implementation linked from [architecture.md](./architecture.md).
