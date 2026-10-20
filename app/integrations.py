"""Optional integrations for ContextOS. Each adapter fails soft when not configured."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from app.config import settings


def integration_status() -> dict[str, Any]:
    status = {"postgres": "managed by API", "slack_events": "configured" if settings.slack_signing_secret else "needs signing secret",
              "slack_reply": "configured" if settings.slack_bot_token else "needs bot token",
              "redis": "not configured", "qdrant": "not configured", "neo4j": "not configured",
              "langgraph": "available" if _has_module("langgraph") else "install dependency",
              "mcp": "available" if _has_module("mcp") else "install dependency",
              "google_calendar": "not configured"}
    if settings.redis_url:
        try:
            import redis
            redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1).ping(); status["redis"]="connected"
        except Exception as e: status["redis"] = f"error: {type(e).__name__}"
    if settings.qdrant_url:
        try:
            from qdrant_client import QdrantClient
            QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None, timeout=2).get_collections()
            status["qdrant"]="connected"
        except Exception as e: status["qdrant"] = f"error: {type(e).__name__}"
    if settings.neo4j_uri:
        try:
            from neo4j import GraphDatabase
            driver=GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)); driver.verify_connectivity(); driver.close(); status["neo4j"]="connected"
        except Exception as e: status["neo4j"] = f"error: {type(e).__name__}"
    if settings.google_calendar_enabled and Path(settings.google_calendar_token_file).exists(): status["google_calendar"]="token file present (runtime verification on use)"
    elif settings.google_calendar_enabled and Path(settings.google_calendar_credentials_file).exists(): status["google_calendar"]="credentials present; run OAuth setup"
    if _has_module("langgraph"): status["langgraph"]="installed; retrieval workflow enabled"
    if _has_module("mcp"): status["mcp"]="installed; run python -m app.mcp_server"
    return status


def _has_module(name: str) -> bool:
    try: __import__(name); return True
    except Exception: return False


def _embed(texts: list[str]) -> list[list[float]]:
    from fastembed import TextEmbedding
    model=TextEmbedding(model_name=settings.embedding_model)
    return [v.tolist() for v in model.embed(texts)]


def upsert_message_vector(message: Any) -> str:
    if not settings.qdrant_url: return "skipped: QDRANT_URL not configured"
    from qdrant_client import QdrantClient, models
    client=QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
    collection=settings.qdrant_collection
    names={c.name for c in client.get_collections().collections}
    if collection not in names:
        client.create_collection(collection_name=collection, vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE))
    vector=_embed([message.text or ""])[0]
    client.upsert(collection_name=collection, points=[models.PointStruct(id=message.id, vector=vector, payload={"message_id":message.id,"team_id":message.team_id,"channel_id":message.channel_id,"text":message.text,"priority":message.priority_label,"created_at":message.message_created_at.isoformat() if message.message_created_at else None})])
    return "indexed"


def search_vectors(question: str, limit: int = 5) -> list[dict[str, Any]]:
    if not settings.qdrant_url: return []
    from qdrant_client import QdrantClient
    client=QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
    if settings.qdrant_collection not in {c.name for c in client.get_collections().collections}: return []
    vector=_embed([question])[0]
    hits=client.query_points(collection_name=settings.qdrant_collection, query=vector, limit=limit, with_payload=True).points
    return [{"message_id":h.payload.get("message_id"),"channel_id":h.payload.get("channel_id"),"priority":h.payload.get("priority"),"text":h.payload.get("text"),"score":h.score} for h in hits]


def sync_message_graph(message: Any) -> str:
    if not settings.neo4j_uri: return "skipped: NEO4J_URI not configured"
    from neo4j import GraphDatabase
    driver=GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    try:
        with driver.session(database=settings.neo4j_database) as session:
            session.run("""MERGE (w:Workspace {id:$team}) MERGE (c:Channel {id:$channel}) MERGE (w)-[:HAS_CHANNEL]->(c)
            MERGE (m:Message {id:$id}) SET m.text=$text, m.priority=$priority, m.created_at=$created
            MERGE (c)-[:CONTAINS]->(m)
            WITH m MERGE (u:User {id:$user}) MERGE (u)-[:WROTE]->(m)""",
            team=message.team_id,channel=message.channel_id,id=message.id,text=message.text,priority=message.priority_label,
            created=message.message_created_at.isoformat() if message.message_created_at else None,user=message.user_id or "unknown")
    finally: driver.close()
    return "synced"


def retrieve_with_langgraph(question: str, fallback_sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Small LangGraph workflow: retrieve context, then return grounded snippets (no LLM key required)."""
    try:
        from typing_extensions import TypedDict
        from langgraph.graph import StateGraph, END
        class State(TypedDict):
            question: str
            sources: list[dict[str, Any]]
            answer: str
        def retrieve(state: State):
            sources=search_vectors(state["question"], 5) or fallback_sources
            return {"sources":sources}
        def compose(state: State):
            lines=[f"[Message #{s.get('message_id')} | {s.get('priority','?')} | channel {s.get('channel_id')}] {s.get('text','')}" for s in state["sources"]]
            return {"answer": "Relevant source messages (verify context before acting):\n"+"\n".join(lines) if lines else "No relevant stored messages found."}
        graph=StateGraph(State); graph.add_node("retrieve",retrieve); graph.add_node("compose",compose); graph.set_entry_point("retrieve"); graph.add_edge("retrieve","compose"); graph.add_edge("compose",END)
        result=graph.compile().invoke({"question":question,"sources":fallback_sources,"answer":""})
        return {"answer":result["answer"],"sources":result["sources"],"mode":"langgraph"}
    except Exception:
        return {"answer":"Relevant source messages (fallback retrieval):\n"+"\n".join(f"[Message #{s.get('message_id')}] {s.get('text','')}" for s in fallback_sources) if fallback_sources else "No relevant stored messages found.","sources":fallback_sources,"mode":"keyword-fallback"}


def create_calendar_event(title: str, start: datetime, duration_minutes: int = 30, description: str = "Created from an approved ContextOS suggestion") -> dict[str, Any]:
    if not settings.google_calendar_enabled: raise RuntimeError("Set GOOGLE_CALENDAR_ENABLED=true after configuring Google Calendar OAuth")
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    scopes=["https://www.googleapis.com/auth/calendar.events"]
    token=Path(settings.google_calendar_token_file); creds=None
    if token.exists(): creds=Credentials.from_authorized_user_file(str(token),scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
        else:
            credfile=Path(settings.google_calendar_credentials_file)
            if not credfile.exists(): raise RuntimeError(f"Missing Google OAuth client file: {credfile}")
            flow=InstalledAppFlow.from_client_secrets_file(str(credfile),scopes)
            creds=flow.run_local_server(port=0)
        token.write_text(creds.to_json(),encoding="utf-8")
    end=start+timedelta(minutes=duration_minutes)
    service=build("calendar","v3",credentials=creds,cache_discovery=False)
    body={"summary":title,"description":description,"start":{"dateTime":start.isoformat()},"end":{"dateTime":end.isoformat()}}
    event=service.events().insert(calendarId=settings.google_calendar_id,body=body).execute()
    return {"event_id":event.get("id"),"htmlLink":event.get("htmlLink"),"status":event.get("status")}
