"""Run as a local MCP server: python -m app.mcp_server"""
from app.database import SessionLocal
from app.models import SlackMessage, Reminder, SuggestedEvent
try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:
    raise SystemExit("MCP SDK missing. Install requirements-integrations.txt first.") from exc

mcp=FastMCP("ContextOS")

@mcp.tool()
def search_slack_messages(query: str, limit: int = 5) -> list[dict]:
    """Search stored Slack messages by keyword and return source IDs and priority labels."""
    terms=[t.lower() for t in query.split() if len(t)>2]
    with SessionLocal() as db:
        rows=db.query(SlackMessage).order_by(SlackMessage.priority_score.desc()).limit(1000).all()
        ranked=sorted(((sum(t in r.text.lower() for t in terms),r) for r in rows),key=lambda x:(x[0],x[1].priority_score),reverse=True)
        return [{"message_id":r.id,"channel_id":r.channel_id,"priority":r.priority_label,"reason":r.reason,"text":r.text} for score,r in ranked[:max(1,min(limit,20))] if score>0]

@mcp.tool()
def list_pending_reminders(limit: int = 20) -> list[dict]:
    """List pending reminders from the ContextOS PostgreSQL database."""
    with SessionLocal() as db:
        return [{"id":r.id,"title":r.title,"due_at":r.due_at.isoformat(),"message_id":r.message_id} for r in db.query(Reminder).filter_by(status="pending").order_by(Reminder.due_at).limit(max(1,min(limit,100))).all()]

@mcp.tool()
def list_scheduling_suggestions(limit: int = 20) -> list[dict]:
    """List meeting suggestions. This tool does not approve or create calendar events."""
    with SessionLocal() as db:
        return [{"id":r.id,"title":r.title,"proposed_start":r.proposed_start.isoformat(),"status":r.status,"message_id":r.message_id} for r in db.query(SuggestedEvent).filter_by(status="suggested").order_by(SuggestedEvent.proposed_start).limit(max(1,min(limit,100))).all()]

if __name__=="__main__": mcp.run()
