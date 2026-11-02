"""Run as a local MCP server: CONTEXTOS_MCP_USER_EMAIL=you@example.com python -m app.mcp_server
The server is bound to ONE ContextOS user; it never returns another user's Slack data."""
import os

from app.database import SessionLocal
from app.models import Reminder, SlackMessage, SuggestedEvent, User
try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:
    raise SystemExit("MCP SDK missing. Install requirements-integrations.txt first.") from exc

EMAIL = os.environ.get("CONTEXTOS_MCP_USER_EMAIL", "").strip().lower()
if not EMAIL:
    raise SystemExit("Set CONTEXTOS_MCP_USER_EMAIL to the ContextOS account this MCP server may read.")
mcp = FastMCP("ContextOS")


def _uid(db) -> int:
    user = db.query(User).filter_by(email=EMAIL).first()
    if not user:
        raise RuntimeError(f"No ContextOS user {EMAIL}")
    return user.id


@mcp.tool()
def search_slack_messages(query: str, limit: int = 5) -> list[dict]:
    """Search the user's stored Slack messages by keyword and return source IDs and priority labels."""
    terms = [t.lower() for t in query.split() if len(t) > 2]
    with SessionLocal() as db:
        rows = (db.query(SlackMessage).filter(SlackMessage.owner_id == _uid(db), SlackMessage.is_deleted.is_(False))
                .order_by(SlackMessage.priority_score.desc()).limit(1000).all())
        ranked = sorted(((sum(t in r.text.lower() for t in terms), r) for r in rows), key=lambda x: (x[0], x[1].priority_score), reverse=True)
        return [{"message_id": r.id, "channel_id": r.channel_id, "priority": r.priority_label, "reason": r.reason, "text": r.text}
                for score, r in ranked[:max(1, min(limit, 20))] if score > 0]


@mcp.tool()
def list_pending_reminders(limit: int = 20) -> list[dict]:
    """List the user's pending reminders."""
    with SessionLocal() as db:
        rows = db.query(Reminder).filter_by(owner_id=_uid(db), status="pending").order_by(Reminder.due_at).limit(max(1, min(limit, 100))).all()
        return [{"id": r.id, "title": r.title, "due_at": r.due_at.isoformat(), "message_id": r.message_id} for r in rows]


@mcp.tool()
def list_scheduling_suggestions(limit: int = 20) -> list[dict]:
    """List meeting suggestions. This tool does not approve or create calendar events."""
    with SessionLocal() as db:
        rows = db.query(SuggestedEvent).filter_by(owner_id=_uid(db), status="suggested").order_by(SuggestedEvent.proposed_start).limit(max(1, min(limit, 100))).all()
        return [{"id": r.id, "title": r.title, "proposed_start": r.proposed_start.isoformat(), "status": r.status,
                 "provisional": r.is_provisional, "message_id": r.message_id} for r in rows]


if __name__ == "__main__":
    mcp.run()
