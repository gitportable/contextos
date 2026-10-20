# ContextOS — Slack ingestion MVP (first vertical slice)

This starter implements the first end-to-end backend slice:

1. FastAPI receives Slack Events API callbacks.
2. It verifies Slack's HMAC signature and timestamp.
3. It deduplicates callbacks using Slack `event_id`.
4. It stores ordinary user messages in PostgreSQL.
5. A transparent rules-based scorer assigns P0–P3 and stores the reasons.
6. `GET /messages` exposes the priority-ordered inbox data.

**This is a development scaffold, not production-ready.** It does not yet implement OAuth/token storage, historical sync, edits/deletions, reminders, MCP, multi-user tenant authorization, or a frontend. Do not expose it publicly with real data until those controls are implemented.

## 1. Prerequisites

- Python 3.11+
- Docker Desktop (for PostgreSQL)
- A Slack workspace where you can create an app
- For local webhook testing, a tunnel such as ngrok or Cloudflare Tunnel

## 2. Start the database

From this folder in VS Code terminal:

```powershell
docker compose up -d
```

## 3. Configure Python

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `SLACK_SIGNING_SECRET` to the Signing Secret from your Slack app. Keep `.env` private and never commit it.

## 4. Run the API

```powershell
uvicorn app.main:app --reload
```

Check:
- Health: http://127.0.0.1:8000/health
- Interactive API docs: http://127.0.0.1:8000/docs
- Inbox endpoint: http://127.0.0.1:8000/messages

The first run creates the MVP tables automatically. For a production application, replace `create_all()` with Alembic migrations.

## 5. Configure Slack Events API

In the Slack API dashboard for your app:

1. Enable **Event Subscriptions**.
2. Set the Request URL to `https://YOUR_PUBLIC_TUNNEL/integrations/slack/events`.
3. Slack validates the URL with a signed `url_verification` request.
4. Subscribe to the bot event `message.channels` for public channels. Add other message scopes only when needed and after reviewing the privacy implications.
5. Under **OAuth & Permissions**, add the minimum bot scope required for the channels you want to read (for public-channel message events, typically `channels:history`; posting is not needed for this ingestion-only slice).
6. Install/reinstall the app to the workspace and invite it to a test channel if required by the selected scopes.

Slack scopes, event subscriptions, and visibility depend on whether the app uses bot or user tokens and which channel types you need. Start with a private test workspace/channel. Never ask users for their Slack password.

## 6. Test the pipeline

1. Start the API and tunnel.
2. Post a test message in a channel the app can access, such as: `Can you review this today? We are blocked.`
3. Open `/messages`. The stored message should show a higher priority than a simple FYI.
4. Try `/messages?priority=P1` to filter.

The endpoint only ingests standard message events in this first pass. It skips bot messages and message subtypes; edit/delete reconciliation is a later step. Slack may retry events, so the event receipt table makes retries idempotent.

## Priority rules in this starter

- Direct action/request language: +20
- Near-term deadline wording: +25
- Blocker/incident wording: +25
- Decision/approval language: +10
- FYI wording and very short messages reduce the score
- P0 is reserved for a critical incident phrase plus blocker language

These are baseline heuristics, not an AI classifier. They should be tested against labeled examples and tuned from user feedback before real-world use. A VIP list, project importance, message/thread context, user-specific preferences, and an LLM classifier are not yet included.

## Current database tables

- `event_receipts`: one row per Slack Events API `event_id`, used for retry deduplication.
- `slack_messages`: canonical MVP message record with team/channel/timestamp, text, score, label, reason, and ingestion times.

## Next implementation milestones

1. Add tests for signature validation, duplicate events, and priority scoring.
2. Implement Slack OAuth install/callback and encrypted per-workspace token storage.
3. Add message edits/deletions and initial history sync with rate-limit handling.
4. Add user identity, workspace authorization, project mapping, handled/snoozed state.
5. Add durable reminders and a worker/outbox pattern.
6. Add MCP tools over authorized stored data (search, thread context, priority, open commitments, propose reminder).
7. Add frontend inbox and feedback-driven scoring.
