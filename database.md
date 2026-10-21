# ContextOS database and storage map

This document explains what ContextOS persists and which store it uses. It describes the current implementation, not a proposed architecture.

## At a glance

| Store | Required? | What it holds | Authority |
|---|---|---|---|
| Relational DB (`DATABASE_URL`; PostgreSQL for deployment) | Yes for normal operation | Accounts, authorized Slack connections, imported messages, projects, reminders, schedule suggestions, durable jobs, and audit/operational records | **Canonical source of truth** |
| SQLite | No for production; used by tests/local tooling | Same relational schema for isolated tests and some local development | Test/development only |
| Redis (`REDIS_URL`) | Optional | Short-lived API response cache and shared request rate-limit counters | Disposable; the relational DB remains authoritative |
| Qdrant (`QDRANT_URL`) | Optional | Embeddings and searchable payloads for eligible Slack messages | Derived, rebuildable search index |
| Neo4j (`NEO4J_URI`) | Optional | Graph representation of workspaces, channels, messages, authors, and projects | Derived, rebuildable graph index |
| Local filesystem | Optional | Google Calendar OAuth client credentials and authorized-user token files | External integration credentials; not relational rows |
| Process memory | Always while processes run | Connection clients, short-lived project matcher cache, and service-status cache | Ephemeral |

The API and worker must use the same relational database. Redis, Qdrant, and Neo4j may be unavailable without preventing canonical message storage; their feature-specific operations can fall back or report unavailable.

## Relational database

`DATABASE_URL` configures SQLAlchemy's database connection. PostgreSQL is expected for deployment and durable multi-process queue operation. SQLite is used in automated tests; SQLite support should not be mistaken for the recommended production configuration. Tables are declared in `app/models.py`; Alembic schema changes live under `migrations/`.

### Identity, sessions, and authorization

| Table | Data stored |
|---|---|
| `users` | ContextOS email, display name, password hash, optional Google subject/avatar, account-active state, and login timestamps |
| `user_sessions` | User/session relationship, SHA-256 hash of the random session cookie token, CSRF token, and expiration timestamps; raw cookie token is not stored |
| `oauth_states` | Hashed, expiring, single-use OAuth state, purpose, initiating user, requested scopes, and optional PKCE verifier |
| `rate_limits` | Database-backed request counters and login lockout windows/strikes when rate-limit handling uses the DB |

### Slack connections and imported data

| Table | Data stored |
|---|---|
| `slack_connections` | ContextOS owner, Slack team/user identity, scopes, connection and sync status, progress, and encrypted Slack access/refresh tokens |
| `slack_conversations` | Per-connection accessible conversation metadata and history/reconcile cursors/checkpoints |
| `slack_users` | Workspace directory cache: Slack user ID, display name, username, optional email, and bot flag |
| `slack_messages` | Canonical per-owner Slack messages: normalized/raw text, team/channel/thread/author fields, timestamps, score/priority, ownership, deletion/handled state, and optional project association |
| `event_receipts` | Slack event IDs and receipt metadata used for replay/deduplication |

Slack access tokens are encrypted before being written to `slack_connections`. The encryption key is configured by `TOKEN_ENCRYPTION_KEY`; losing or changing it without a planned key rotation can make stored tokens unusable.

### User-facing product data and auditing

| Table | Data stored |
|---|---|
| `projects` | Owner-scoped project name/slug/keywords, active/archive state, message and priority counts, and summary metadata |
| `reminders` | Reminder title, due date, status, optional source message, assigned Slack people, and automatic-extraction key |
| `suggested_events` | Meeting title, proposed time/duration, provisional flag, source text/message, status, assignees, and extraction key |
| `summaries` | Generated/extractive project summaries and daily briefings, provider metadata, and source message IDs |
| `assistant_notes` | Asked question, answer, and the cited source-message IDs |
| `outbound_actions` | Approved Slack action request/audit data, status, response/error, and idempotency key |

### Worker and observability data

| Table | Data stored |
|---|---|
| `processing_jobs` | Durable queue jobs, owner/connection context, serialized job payload, retry/lock state, and completion/error metadata |
| `pipeline_events` | Processing stages, status/detail, owner/team/message references, and event timestamps |
| `worker_heartbeats` | Worker identity and latest heartbeat/job counters |

Rows containing user data are scoped by `owner_id` directly or through their owned Slack connection. API queries should always enforce the authenticated user's ownership.

## Redis

Redis is configured by `REDIS_URL` and is optional:

- Cached JSON responses use owner-prefixed `contextos:u<owner_id>:...` keys and short expirations.
- Shared rate-limit counters use `contextos:rl:...` keys with expiry windows.
- Cache invalidation removes a user's response-cache keys; cache entries may be lost or evicted safely.
- If Redis is unavailable, the cache is bypassed and rate limiting falls back to database-backed counters where configured by the implementation.

Do not treat Redis as the durable message store or queue.

## Qdrant

Qdrant is configured by `QDRANT_URL`, optional `QDRANT_API_KEY`, and `QDRANT_COLLECTION` (default collection name: `contextos_messages`). Eligible message text is embedded with the configured embedding model; the current collection uses 384-dimensional cosine vectors.

Each point uses the relational message ID as its point ID and stores payload fields including `message_id`, `owner_id`, `team_id`, `channel_id`, `text`, `priority`, `author`, `project_id`, and `created_at`. Semantic queries must filter by `owner_id`; project searches may additionally filter by `project_id`.

Qdrant is an index, not the canonical record. It can be reindexed from eligible relational messages. A Qdrant outage does not prevent the relational message row from being stored, although semantic search may be unavailable.

## Neo4j

Neo4j is configured by `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, and `NEO4J_DATABASE` (default database name: `neo4j`). The integration creates/updates graph entities:

- Nodes: `Workspace`, `Channel`, `Message`, `User`, and `Project`.
- Relationships: `HAS_CHANNEL`, `CONTAINS`, `WROTE`, `ABOUT`, `PARTICIPATES_IN`, and `DISCUSSES`.

Workspace/channel/person/project identifiers include owner or team context to scope the graph. Message text, priority, creation time, and owner are copied to graph nodes for graph queries. Neo4j supports people/project/channel views; SQL remains the fallback and source of truth.

## Files and external providers

- Google Calendar integration reads the client credential file configured by `GOOGLE_CALENDAR_CREDENTIALS_FILE` and writes/reads an authorized-user token file configured by `GOOGLE_CALENDAR_TOKEN_FILE` (defaults: `credentials.json` and `token.json`). Protect these files like credentials; they are not encrypted by the application's Slack-token storage path.
- LLM providers (such as Groq/Gemini) are external APIs, not application databases. ContextOS may send message snippets, questions, and summary context to the configured provider to generate answers or summaries. Provider configuration, quotas, and privacy terms are separate from database persistence.
- Slack remains the upstream source for Slack messages. Imported message content is persisted by ContextOS according to the application's retention and deletion behavior.

## Backups, retention, and rebuilds

- Back up the relational database; it is required to restore accounts, canonical messages, projects, reminders, suggestions, and pending jobs.
- Preserve `TOKEN_ENCRYPTION_KEY` backups alongside the encrypted database backup, using a separate secure secret-management process.
- Qdrant and Neo4j are derived indexes and can be rebuilt from relational records where indexing data is available; rebuilding may require time and configured embedding/graph services.
- Redis does not need a durable backup for application correctness.
- Google Calendar credential/token files require their own secure backup or reauthorization procedure.
- Retention settings control message and pipeline-event lifecycle; review configuration and `app/worker.py` before setting production retention expectations.

For routes that read or change these data, see [api.md](./api.md). For ingestion and component flow, see [architecture.md](./architecture.md).
