# ContextOS

ContextOS is a multi-user work assistant. Each person signs in to a ContextOS account, connects their own Slack, Gmail, or read-only Google Calendar, and works from a Workspace (Brief + Ask) and a Ledger of confirmed commitments, follow-ups, and meetings. The deployment owner has a separate admin view for system-wide pipeline and configuration status.

The app is an actively developed MVP. Review its privacy, security, and operational requirements before exposing it to real users.

## What is implemented

- Email/password registration and sign-in, optional Google sign-in, server-side sessions, and account/session settings.
- Per-ContextOS-user Slack OAuth connections, Slack history sync, signed Events API intake, and a PostgreSQL-backed worker queue.
- A Workspace with a capped Brief, Ask with message citations, actual Calendar events, and a Ledger with owner-scoped lifecycle and source links.
- A legacy message inbox and project/people views, reachable from Workspace; ingestion does not turn message text into unconfirmed reminders or guessed meetings.
- Slack directory search and assignment of Slack people to reminders and schedule suggestions.
- Optional Redis, Qdrant, Neo4j, LLM providers, and Google Calendar integration.
- Owner-only pipeline overview, event feed, trace, and integration status.

See [explanation.md](./explanation.md) for how these pieces work, [architecture.md](./architecture.md) for components and data flow, [api.md](./api.md) for HTTP endpoints and request conventions, and [database.md](./database.md) for what is stored in each database/service.

## Requirements

- Python 3.11 or newer.
- PostgreSQL, either hosted (for example, Supabase) or installed locally. Docker is not required.
- A Slack app with OAuth configured for workspaces you intend to support.
- A public HTTPS tunnel or deployment URL for Slack callbacks/events during local development.

## Local setup (PowerShell)

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

The standard requirements install includes the Qdrant, Neo4j, MCP, and Google integration libraries. For a Docker-free setup, use your hosted Supabase PostgreSQL database and keep your hosted Qdrant and Neo4j endpoints in `.env`. Docker and the local services in `docker-compose.yml` are optional; do not run `docker compose up` when using hosted services.

Set the required values in `.env` before starting:

- `DATABASE_URL`: PostgreSQL connection string.
- `PUBLIC_BASE_URL`: the exact public HTTPS origin used in the browser (for example, your current ngrok URL, without a trailing slash).
- `ADMIN_EMAIL`: the ContextOS login email that owns the deployment.
- `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, and `SLACK_SIGNING_SECRET`.
- `TOKEN_ENCRYPTION_KEY`: generate with `python -m app.crypto`; keep it stable while encrypted tokens are stored.

For Supabase, copy the PostgreSQL connection string from **Project Settings > Database** into `DATABASE_URL`. This app connects directly to PostgreSQL; the Supabase project URL and publishable API key are not database connection credentials. Keep the database password private. Run `python -m alembic upgrade head` against the configured database before starting the API and worker.

Optional:

- Google sign-in uses `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
- Google Calendar uses the same OAuth client. Add `<PUBLIC_BASE_URL>/integrations/google-calendar/callback` as an authorised redirect URI (or set `GOOGLE_CALENDAR_REDIRECT_URI`). Each user connects their own calendar from Settings; ContextOS requests read-only access for assistant schedule lookups.
- Configure LLM keys (`GROQ_API_KEY` and/or `GEMINI_API_KEY`) for generated answers and summaries. Without a provider, the app uses non-LLM fallbacks.
- Redis, Qdrant, Neo4j, and Google Calendar can be left unconfigured when not used.

Never commit `.env` or share its secrets. The `.env.example` file is a template, not a source of credentials.

## Configure Slack

In the Slack app configuration:

1. Under **OAuth & Permissions**, add the redirect URL:
   `https://YOUR_PUBLIC_HOST/integrations/slack/callback`
2. Set `SLACK_REDIRECT_URI` to that exact URL, or leave it empty to derive it from `PUBLIC_BASE_URL`.
3. If using Event Subscriptions, set the Request URL to:
   `https://YOUR_PUBLIC_HOST/integrations/slack/events`
   These URLs are different: OAuth returns through `/callback`; Slack event delivery uses `/events`.
4. Enable distribution/installation for the workspaces that should be allowed to authorize the app. Workspace administrators may need to approve it.

Each user must sign in to ContextOS and authorize their own Slack workspace. Slack can reuse whichever Slack account is already active in that browser, so the user should check the workspace selector on Slack's consent screen. ContextOS associates the OAuth result with the signed-in ContextOS user; it does not choose a Slack workspace from an email address.

The browser's default read preset requests user scopes for channels the user can access, direct messages, and directory names. Private channels/group DMs and sending replies require additional user choices. Avoid adding permissions that the product does not need.

## Database and run

Apply migrations to an existing database:

```powershell
python -m alembic upgrade head
```

For a brand-new development database, the app can create missing tables when `AUTO_CREATE_TABLES=true`. Migrations are the preferred way to upgrade a database. The legacy-schema reset script deletes development data; use it only when that data is disposable.

Start the API and worker in separate terminals:

```powershell
python -m uvicorn app.main:app --reload --port 8000
python -m app.worker
```

Open the app through `PUBLIC_BASE_URL` when using OAuth or a tunnel. Useful endpoints:

- `/health` — database-backed health check.
- `/docs` — interactive OpenAPI docs in development only.
- `/api/config` — safe public UI feature flags.

The worker is required to process queued history sync, event, and maintenance jobs.

## Tests

Install development dependencies and run the tests:

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

Some tests require external services to be disabled or configured with isolated test values; do not point test runs at a production database.

## Deployment notes

### Deploying the API to Vercel with Supabase Postgres

The URL `https://eeydzmanaorwpfbdpyrg.supabase.co` is the Supabase API/project URL, not a PostgreSQL connection string. In Supabase, open **Project Settings > Database > Connection string**, select the **Transaction pooler** option, and copy its URI. Use that complete URI as Vercel's `DATABASE_URL`; append `?sslmode=require` if it is not already present. The database password is available/reset in the Supabase database settings. Never put it in source control or in a public client variable.

1. In Vercel, import this repository and set the project root to `contextos-slack-mvp` (the directory containing this README and `requirements.txt`).
2. Add these Vercel environment variables:
   - `DATABASE_URL`: the Supabase PostgreSQL pooler URI.
   - `APP_ENV=production`
   - `AUTO_CREATE_TABLES=false`
   - `PUBLIC_BASE_URL=https://YOUR_VERCEL_DOMAIN` (no trailing slash; use the stable production domain).
   - `ADMIN_EMAIL`, `TOKEN_ENCRYPTION_KEY`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, and `SLACK_SIGNING_SECRET`.
   - Add any optional provider credentials you use, such as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
3. Before serving traffic, apply the schema once from a trusted machine using the same `DATABASE_URL`:

   ```powershell
   python -m alembic upgrade head
   ```

4. Deploy. The included Vercel configuration routes the app to FastAPI and includes the static UI files. Update Slack's OAuth redirect URL to `https://YOUR_VERCEL_DOMAIN/integrations/slack/callback` and its Events API URL to `https://YOUR_VERCEL_DOMAIN/integrations/slack/events`.

Production database connections use a small per-instance pool to reduce connection pressure from serverless instances. Use Supabase's pooler URI rather than a direct database URI for Vercel.

Vercel serves the HTTP API, but it does not run the app's continuous PostgreSQL queue worker. Slack sync and other queued jobs will not run unless you also run `python -m app.worker` as a separate persistent process with the same environment variables (including `DATABASE_URL`) on a worker host. Use a host that supports a continuously running Python process; do not run the worker as a Vercel function.

Before production, use HTTPS, a strong and backed-up token-encryption key, PostgreSQL backups, correct proxy configuration, restricted registration, monitoring, dependency pinning, and reviewed Slack scopes. Set `APP_ENV=production` to disable demo functionality and the interactive API docs. Keep credentials in deployment environment variables; do not copy secrets into documentation or source control.

## Documentation

- [API reference and HTTP request flow](./api.md)
- [Application explanation](./explanation.md)
- [Architecture and data lifecycle](./architecture.md)
- [Database and storage map](./database.md)
