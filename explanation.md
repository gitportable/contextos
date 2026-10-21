# ContextOS application explanation

ContextOS turns messages from a user's authorized Slack workspace into a personal, searchable work dashboard. The application has a browser UI, a FastAPI HTTP API, a relational database (PostgreSQL in the normal deployment), and a separate worker process. Optional services add caching, vector search, graph queries, and generated text. See [database.md](./database.md) for what each store contains.

## User and admin experience

### ContextOS account

Users register with an email and password or sign in through Google when configured. Successful sign-in creates a server-side session and sets an HTTP-only cookie. Account settings expose profile, password/session controls, and the user's Slack connections. There is no separate admin login.

The deployment owner is identified by the configured `ADMIN_EMAIL`. Admin status belongs to that ContextOS account; Slack authorization does not grant admin access. Admin-only routes provide system-level pipeline and integration visibility.

### Slack connection

After ContextOS sign-in, a user starts Slack OAuth from the UI. The server creates a single-use OAuth state tied to that ContextOS user, redirects to Slack, validates the state on callback, exchanges the authorization code, and encrypts returned tokens before saving the workspace connection. Connection records are owned by the initiating ContextOS user.

Slack consent uses the account/workspace currently active in the user's Slack browser session. ContextOS does not map an email address to a Slack workspace. Users must verify the Slack workspace selector before granting access. A Slack app also needs distribution/approval configured for any workspace outside its development workspace.

Connections with the configured user scopes can read the user's conversations and support optional features such as people assignment. Replying requires the explicit Slack write permission and user approval for each outgoing reply. Users can connect multiple workspaces, sync, reconnect, or disconnect from Account settings.

### Main UI features

- **Today:** important messages, due reminders, schedule suggestions, items awaiting another person's response, and a daily briefing.
- **Inbox:** message list with priority, search, channel/project filters, and actions.
- **Projects:** projects discovered from recurring message keywords, explicit project/channel names, and repeated structured message-title prefixes. Users can also preview stored messages by keyword and manually add a project.
- **Commitments and schedule:** reminders and meeting suggestions extracted from messages or entered by a user. Users can assign people from their own connected Slack directory.
- **Ask:** a question/answer endpoint that retrieves user-owned messages and summaries and returns source citations. An LLM is optional.
- **Account settings:** profile, sessions, password where applicable, and Slack workspace connections.
- **Admin/Data pipeline:** owner-only operational overview, event feed, message trace, and integration status.

## How Slack data becomes an app feature

There are two primary Slack ingestion paths:

1. **History sync:** after successful OAuth, ContextOS queues a sync job. The worker uses the user's Slack token to enumerate accessible conversations and page through history.
2. **Events API:** Slack sends signed HTTP events to `/integrations/slack/events`. The API verifies Slack's signature and timestamp, records the event id to prevent replay duplication, queues work, and responds. The worker then processes the event for relevant connected users.

Both paths use the application's message-ingestion pipeline. The pipeline normalizes and deduplicates messages, stores them with a user and connection owner, scores priority, extracts reminder/meeting candidates, associates messages with projects, and attempts optional indexing. PostgreSQL remains the canonical store; optional integrations are not required to store a message. Calendar-style messages with explicit dates and time ranges are parsed into fixed schedule suggestions; an event scheduled for today is listed under Today → Due today.

The worker is a separate process (`python -m app.worker`). The API enqueues work but does not perform long-running history imports in the HTTP request. Queue state and checkpoints are persisted in the relational database, allowing sync work to resume after transient failures. Existing messages are reprocessed during sync/reconcile; project discovery can be triggered with Re-scan messages.

## Optional integrations

- **Redis:** short-lived response cache and shared API rate-limit counters; rate limiting can use the relational database when Redis is unavailable.
- **Qdrant:** derived semantic index of eligible Slack messages for retrieval. Relational SQL/keyword search remains available without it.
- **Neo4j:** derived graph for workspace, channel, message, person, and project relationships. SQL-backed project details remain available without it.
- **Groq/Gemini:** generated summaries, briefings, drafts, and answers, within configured budgets. Deterministic/extractive behavior is used when unavailable.
- **Google Calendar:** optional event creation from an approved suggestion, using the configured calendar integration.

Availability and behavior depend on configuration. Pipeline status reports optional-service failures; relational message storage remains the source of truth. Data sent to configured LLM providers is governed by the LLM provider configuration and privacy terms; LLM output is not the canonical message store.

## Security and user isolation

- Every authenticated request resolves a ContextOS user from the server-side session cookie.
- State-changing browser requests require the session's CSRF token and are checked against allowed origins.
- Slack OAuth state is single-use and bound to the initiating ContextOS user.
- Slack tokens are encrypted at rest using `TOKEN_ENCRYPTION_KEY`.
- Message, project, reminder, schedule, action, and Slack-connection queries are scoped to the current user. Slack assignees are accepted only from that user's connected workspaces.
- Slack replies require a user-approved request and are associated with that user's active connection.
- Slack Events API requests use Slack signature verification and replay-time checks.

These controls do not replace a production security review, proper secret management, backups, or monitoring.

## Current limitations

- The application does not infer a user's Slack identity from their ContextOS email. Each person must authorize the intended Slack workspace.
- Slack app distribution and workspace approval are configured in Slack, outside this codebase.
- Availability of private conversations and other data depends on the scopes the user granted and Slack workspace access rules.
- Google Calendar uses local credentials/token configuration; it is not a full per-user Google Calendar OAuth model.
- Development/demo behavior is controlled by application configuration and should be disabled/reviewed before deployment.
- Optional external services and LLM providers can fail or impose quotas; the API and worker should be monitored.

For setup and run commands, see [README.md](./README.md). For HTTP endpoints and examples, see [api.md](./api.md). For system-level data flow and persistence, see [architecture.md](./architecture.md) and [database.md](./database.md).
