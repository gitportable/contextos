# ContextOS architecture

How a person goes from "open the site" to "my Slack is sorted into projects with summaries", one feature at a time.
Everything below describes what the code in `app/` actually does.

```
 Browser (static SPA, no inline JS)
    │  cookie session + CSRF header
    ▼
 FastAPI (app.main) ──────────────► PostgreSQL  ◄── source of truth AND durable job queue
    │  Slack events / OAuth callbacks        ▲
    │                                        │ SELECT … FOR UPDATE SKIP LOCKED
    └──► enqueue job ───────────────────► Worker (python -m app.worker)
                                              │  ingest → score → extract → project link → discover → summarise → retention
                                              ├──► Redis   (rate limits, cache, progress, locks)         optional
                                              ├──► Qdrant  (semantic search: P0–P2 messages + summaries)  optional
                                              ├──► Neo4j   (people ↔ projects graph)                      optional
                                              └──► LLM     (any OpenAI-compatible API, 3 usage gates)     optional
```

PostgreSQL is the only required store. If Redis, Qdrant, Neo4j or the LLM are missing or down, the app degrades (per-process
rate limits, no semantic search, no graph, extractive summaries) and never fails a request because of them.

---

## 1. Login

**Google (primary).** `/auth/google/start` creates `state`, `nonce` and a PKCE verifier, stores them in `oauth_states`
(single use, 10 min), sets a random *binder* cookie and redirects to Google. `/auth/google/callback` checks the state is
unused, unexpired and was issued to **this browser** (binder cookie hash), exchanges the code with the PKCE verifier over
TLS, then validates `iss, aud, azp, exp, iat, nonce, sub, email_verified` and the email shape. The user is found by
`google_sub`, else by email, else created. Pre-hijack defence: if the email was registered with a password but never
verified, that password is wiped and its sessions revoked when the real owner signs in with Google. A different Google
account claiming an already-linked email is refused.

**Email + password (fallback, can be switched off with `ALLOW_PASSWORD_AUTH=false`).** Email must pass `auth.validate_email`
(ASCII only, one `@`, dot-atom local part, DNS-shaped domain, alphabetic TLD) — the browser applies the same rules live so
users see the problem before submitting. Passwords: ≥10 characters, hashed with scrypt, constant-time compare, and an
identical error for "unknown user" and "wrong password".

**Rate limiting** (`app/ratelimit.py`, Redis `INCR`+TTL, in-process fallback): per-IP login and registration windows;
per-(email, IP) failure counter that, after 5 failures, starts an exponential lock-out. A locked account refuses even the
correct password (no oracle) and returns `retry_after`, which the UI turns into a countdown. `TRUSTED_PROXY_HOPS` selects the
Nth-from-right `X-Forwarded-For` entry, so behind ngrok the limit sees the real visitor, and a spoofed header can't pick the key.

**Sessions.** A random token is stored only as a hash; new token on every login (no fixation); `HttpOnly`, `SameSite=Lax`,
`Secure` on https; capped per user (`MAX_SESSIONS_PER_USER`, oldest dropped). Writes require the `X-CSRF-Token` header **and**
a same-origin `Origin`. Every authenticated call also passes a per-user API limit.

## 2. Connecting Slack and choosing permissions

After sign-in a user with no workspace sees onboarding with three plain-language levels (stored as
`slack_connections.access_level`, scopes derived from `ACCESS_LEVELS` in `config.py`):

| Level | What it can read | Slack scopes |
|---|---|---|
| Public channels only (`essential`) | public channels the user is in | `channels:*`, `users:read` |
| All my conversations, read-only (`recommended`, default) | + private channels, DMs, group DMs | + `groups:*`, `im:*`, `mpim:*` |
| + replies I approve (`with_replies`) | same | + `chat:write` |

Users start light and can upgrade later in Settings (re-consent). The flow: `/integrations/slack/connect?level=` →
state row → Slack → `/callback` → state consumed, token exchanged, **encrypted with Fernet** and saved → initial sync queued.
The callback verifies the Slack identity doesn't collide with another ContextOS account. Disconnect revokes the token at
Slack and optionally erases imported messages; "delete my account" (`DELETE /api/account`, email confirmation) does the same
for everything.

### 2b. Connecting Gmail (second source)

Settings / onboarding → "Connect Gmail" → `/integrations/gmail/connect` → Google consent for **`gmail.readonly` only**
(`access_type=offline`, state bound to the signed-in user, single use) → `/integrations/gmail/callback` → the refresh and
access tokens are Fernet-encrypted. A Gmail account is stored as a `slack_connections` row with `provider='gmail'`
(`team_id = gmail:<address>`), so status, sync, disconnect (revokes at Google), purge, retention and account deletion are shared
code. Add `<PUBLIC_BASE_URL>/integrations/gmail/callback` as a second redirect URI in the Google OAuth client and enable the Gmail API.

Sync (`gmail.py`): `messages.list` with `-category:promotions/social/forums -in:spam -in:trash after:<date>` (first run 14 days;
later runs resume from the last finish minus 6 h, idempotent), `messages.get(format=full)`, capped at 400 per run. Each email
maps to the Slack shape: thread → conversation (name = subject, `conv_type='email'`), message → row with a `slack_ts` derived
from Gmail's `internalDate` + id hash. Quoted replies/signatures are trimmed. Priority: an email addressed **To me** by a human
counts like an @mention; anything with `List-Unsubscribe`/`Precedence: bulk` or a no-reply style sender is treated as automated
(lower score). Email is read-only: the reply endpoint refuses it. Expired/revoked access marks the connection `needs_reauth`.

**Cross-app projects.** Discovery and linking already work on all of a user's messages regardless of source, so "Phoenix" in a
Slack channel and "Re: Phoenix cutover" in Gmail become one project folder; summaries cite messages from both, the same person
on both apps is merged in the People list, and the inbox defaults to *Important* (P0–P2) with All apps / Slack / Gmail filters.

## 3. Ingestion: two ways in, one pipeline

* **History sync** (`slack_sync.py`): conversations are listed, each paged with Slack's rate-limit handling (long `Retry-After`
  waits are rescheduled instead of blocking the worker). Each page logs a `sync_page` event and updates Redis progress.
* **Live events** (`/integrations/slack/events`): signature (HMAC + timestamp) verified → `received` event → durable job
  (`queued`, `dedupe_key` unique while queued/processing, so Slack retries can't double-process) → worker fans the event out to
  every connected member of that team (`fanned_out`).

`process_message` normalises the text, applies edits/deletes (a compacted message can't be resurrected by an edit), and
upserts by (connection, channel, ts). Everything is scoped by `owner_id`.

## 4. Scoring and confirmed work

`priority.py` scores messages from structural signals; it does not infer a commitment from the message's wording.
`item_ranker.py` ranks user-confirmed Ledger items with a transparent cold-start rules-only score and explanation. Its
features and parameters are not presented as trained or calibrated. The Brief is capped at seven items. Source links are
exact message IDs stored in `item_sources`, not probabilistic membership masks. Only actual Google Calendar events are
shown in the Workspace schedule rail; message text does not create reminders or guessed meetings. Nothing is posted
anywhere automatically; outbound replies still require explicit user approval.

## 5. Retention tiers (data lifecycle)

| Tier | Stored | Embedded (Qdrant) / graphed |
|---|---|---|
| P0–P2 | full text, up to `RETENTION_KEEP_DAYS` (180) | yes |
| P3 | full text 7 days, then a ≤200-char snippet, deleted at 30 days | never |
| Cap | `MAX_MESSAGES_PER_USER`: oldest P3, then P2, pruned; P0/P1 never | – |

`retention.py` runs daily from the worker, in bounded chunks. Deleting a message also removes its project links, exact
item-source links, and index entries; confirmed items remain while their source IDs are detached. Manually created
reminders and schedule suggestions are kept. Table partitioning by month is the next step at
scale and is **not implemented**.

## 6. Projects (derived from the messages, named by what's discussed)

`projects.py`, no LLM needed: candidate phrases (capitalised names, repeated bigrams, hashtags/channel names) are scored by
messages, distinct days, authors and conversations inside a 30-day window; thresholds (`project_min_messages`, `project_min_days`)
filter one-off chatter; overlapping candidates are merged so "Phoenix" and "Phoenix cutover" become one project named by the
dominant term. Messages are linked through `message_projects`. Users can rename, merge, archive, create manually; archived
projects are never re-discovered. Discovery runs after ingestion settles and every `project_discover_minutes` for users with new messages.

### 6b. What Qdrant and Neo4j add to the algorithm (and what happens without them)

| Signal | With the service | Without it |
|---|---|---|
| **Semantic project links** (Qdrant) | Important (P0–P2) messages no keyword caught are embedded and linked to the project whose *summary* is closest (cosine ≥ `SEMANTIC_LINK_MIN_SCORE`, default 0.50). Stored with `hits=0` so they're distinguishable; runs after discovery and after each new summary. | Keyword links only. |
| **Assistant retrieval** (Qdrant) | Question → nearest summaries (project routing when no keyword matches) and nearest messages, always tenant-filtered and re-loaded from PostgreSQL by owner. | Keyword ranking. |
| **Key collaborators** (Neo4j) | People who write about ≥2 projects (`Person–WROTE→Message–ABOUT→Project`) get their recent messages with some real signal (score ≥25) raised by +10, capped below P1, once, with the reason shown ("from a key collaborator"). Also powers "who to ask" and "projects that share people". | Same question answered from PostgreSQL (grouped by display name, so a person on Slack and Gmail counts once). |

Project discovery also merges *facets*: a phrase whose messages are ≥60% inside a stronger project becomes an alias of it
instead of a duplicate project.

## 7. Summaries and the LLM

`summaries.py` writes **versioned** summaries. Inputs: the most recent plus highest-priority messages of the project (capped);
skipped when the input hash is unchanged or fewer than 3 new messages arrived. Each key point / open item must cite message
ids; **uncited or invented ids are dropped**. Message text is fenced as untrusted data in the prompt. Without an LLM, or when
rate limited, a labelled *extractive* summary is produced and upgraded automatically later.

`llm.py` calls any OpenAI-compatible endpoint (Groq/Gemini free tiers are presets in `.env.example`) behind three gates:
requests/minute (global), calls/day (global), calls/day (per user). The per-user gate is checked first so one user can't burn
the shared quota. Failed provider calls still count (providers may bill them). Set the numbers slightly under your provider's
console values; limits and model IDs change often and **were not verified live** here.

## 8. Assistant

`/assistant/ask`: detects project names in the question, retrieves matching messages (Postgres text search, plus Qdrant when
enabled, filtered by owner and project) and project summaries, and answers with the LLM or extractively, always returning the
source messages. Owner scoping is mandatory in every query and every vector/Cypher filter.

## 9. Landing page, sample data, feature check

The Assistant is the landing page: stats strip, suggested questions built from your project names, answers with sources and a
"Who to ask" panel. With no data it offers **Load sample data** (also pinned in the sidebar in development) and Connect Slack/Gmail.
The Pipeline page has a **Feature check** (`/api/selfcheck`): ~18 lines (worker, Slack, Gmail, scoring, reminders, projects,
cross-app projects, summaries, LLM, citations, Qdrant vectors, semantic links, Neo4j graph counts, collaborator boost, Redis,
events), each verified against your real rows or a real service query and marked ok / warn (on but nothing yet) / off / fail.

## 9b. Pipeline page (observability) — ADMIN ONLY

The Pipeline tab, `/api/pipeline/*`, `/api/selfcheck`, `/api/integrations`, `/queue/*` and a message's "Pipeline trace" are for
administrators. Set `ADMIN_EMAILS=you@x.com,other@x.com`; in production the address must also be Google-verified, so a
password sign-up with someone else's e-mail cannot become admin. With the list empty, in development the first account created is
the admin; in production nobody is (startup warns). Everyone else does not see the tab and the server answers `403 admin_only`.
Ordinary users get their own "Smart features" status and a sample-data switch in **Settings** instead.

Each stage writes a row to `pipeline_events` (`login`, `slack_connected`, `received`, `queued`, `fanned_out`, `sync_page`,
`ingested`, `scored`, `extracted`, `indexed_vector`, `indexed_graph`, `project_linked`, `summarized`, `answered`, `retention`).
The page polls `/api/pipeline/overview` (per-layer counts, worker heartbeat, queue depth, LLM usage, service status) and
`/api/pipeline/events?since_id=`. Clicking a message's "Pipeline trace" shows only that message's journey. Events are pruned
after 7 days and capped per user. A demo workspace (321 messages, 5 projects, noise, edits/deletes, bot alerts) can be loaded and
removed from this page in development.

## 10. Redis, Qdrant, Neo4j (and Supabase)

PostgreSQL can be Supabase: set `DATABASE_URL` to its direct (or session-pooler) string, run `alembic upgrade head`, and **enable
RLS with no policies** on every table (Supabase can expose `public` through its REST API; ContextOS does its own auth and
never uses that API). Migrations `0004` and `0005` fill columns missing from older schemas and make the unused legacy OAuth
scopes column nullable without removing existing data. Avoid transaction-mode pooling for the worker (row-lock queue).
Not tested on Supabase itself.
Qdrant Cloud and Neo4j Aura free tiers work with `QDRANT_URL`/`QDRANT_API_KEY` and `NEO4J_URI`/`USER`/`PASSWORD`.

* **Redis**: rate-limit counters, short-lived cache (hashed keys, invalidated on writes), sync progress, job locks. 15 s back-off after a failure.
* **Qdrant**: one collection, mandatory `owner_id` filter, payload `kind` (message/summary) and `project_ids`; summaries live at point id `10¹² + project_id`.
* **Neo4j**: `Person–WROTE→Message–ABOUT→Project`, every query constrained by owner; answers "who works on X" and "which projects share people".

### 10b. What Qdrant and Neo4j do for the user (all tenant-scoped, all with a PostgreSQL fallback)

| Feature (where you see it) | Engine | How it works | Without the engine |
|---|---|---|---|
| **Search by meaning** (Inbox search box) | Qdrant | `/api/search`: keyword hits + nearest message vectors, fused with reciprocal-rank fusion; each result is labelled *words / meaning / both*. | Word matching only |
| **Related** button on every message | Qdrant | Nearest vectors to that message, excluding itself; flags results from the *other* app (Slack ↔ Gmail). | Other messages in the same project |
| **Duplicate project suggestions** (Projects page) | Qdrant | Cosine similarity between project *summary* vectors ≥ `MERGE_SUGGEST_MIN_SCORE`; one click merges the smaller into the larger. | Projects sharing ≥50% of their messages |
| **Urgency echo** (priority reason: "similar to an urgent message") | Qdrant | A recent P2/P3 message ≥ `ESCALATION_MIN_SCORE` similar to a P0/P1 one gets +`ESCALATION_BOOST`, capped below P1, once, with the reason shown. | No boost |
| **Semantic project links**, **assistant retrieval** | Qdrant | (already described in 6b / 8) | Keyword only |
| **People page** (connectors, projects per person, who works with whom) | Neo4j | `Person-WROTE→Message-ABOUT→Project`; "works with" = shared projects via the graph; names merged across Slack and Gmail. | Counted from PostgreSQL by display name |
| **Single point of failure** badge on projects | Neo4j | One person wrote ≥ `DEPENDENCY_SHARE` of ≥ `DEPENDENCY_MIN_MESSAGES` messages in a project. | Same rule computed in PostgreSQL |
| **Who to ask**, **projects sharing people**, **key-collaborator boost** | Neo4j | (already described in 6b) | PostgreSQL grouping |

**Switching them on.** `docker compose up -d` starts PostgreSQL, Redis, Qdrant (6333) and Neo4j (Bolt 7687, browser 7474).
`.env.example` already points at those addresses. Existing messages are caught up automatically: the worker enqueues an
`index_backfill` job per user at most once an hour (it does nothing when the indexes are complete), Settings → *Smart features* →
**Rebuild from my messages** does it on demand, and `python scripts/backfill_indexes.py [--email x] [--force-graph]` does it from
a terminal. Neo4j gets uniqueness constraints on first write so `MERGE` stays fast. Only P0–P2 messages are embedded/graphed (P3 chatter never is).

## 11. Security model (summary)

CSP `script-src 'self'` (no inline script), nosniff, frame deny, `no-store` on API responses, same-origin check on writes,
1 MB body cap, generic error bodies (no stack traces), API docs off in production, startup refuses unsafe production config
(`production_problems()`), tokens encrypted at rest, all data rows owner-scoped, UI renders data via `textContent` only.

## 12. Operations

`alembic upgrade head` (0002 is idempotent and upgrades pre-Google databases in place); `python -m app.worker` must run
(the UI warns when no heartbeat); `/health` is liveness, `/ready` checks the database. Worker handles SIGTERM gracefully.

## 13. Known limits

* People are matched across apps by display name only; identical names of different people would be merged in the People list.
* Google, Gmail, Slack, the LLM provider, Qdrant and Neo4j were exercised with mocks/in-memory clients, not live accounts.
  PostgreSQL 16 and Redis were tested for real. The v4 changes (admin gate, `intelligence.py`, People page, docker-compose) were
  written without being able to install the dependencies or run the test suite: run `pytest` once before relying on them.
* Docker image tags in `docker-compose.yml` were not pulled when written; use a nearby tag if one is missing.
* The "urgency echo" and "duplicate project" thresholds are starting points. Tune them in `.env` after looking at real data.
* Project naming is heuristic: occasionally a generic phrase ("Capacity Plan") becomes a project; archive it and it stays gone.
* Monthly partitioning, per-user LLM budgets by plan, and email verification for password sign-ups are future work.
