"""Optional integrations for ContextOS. Each adapter fails soft when not configured."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from app.config import settings


def integration_status() -> dict[str, Any]:
    status = {"postgres": "managed by API", "slack_events": "configured" if settings.slack_signing_secret else "needs signing secret",
              "slack_oauth": "configured" if settings.slack_oauth_configured else "needs SLACK_CLIENT_ID/SECRET",
              "token_encryption": "configured" if settings.token_encryption_key else "needs TOKEN_ENCRYPTION_KEY",
              "slack_redirect_uri": settings.redirect_uri,
              "redis": "not configured", "qdrant": "not configured", "neo4j": "not configured",
              "langgraph": "available" if _has_module("langgraph") else "install dependency",
              "mcp": "available" if _has_module("mcp") else "install dependency",
              "google_calendar": "not configured"}
    if settings.redis_url:
        try:
            import redis
            redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1).ping(); status["redis"]="connected"
        except Exception as e: status["redis"] = f"error: {type(e).__name__}"
    if settings.qdrant_enabled:
        try:
            from qdrant_client import QdrantClient
            QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None, timeout=2).get_collections()
            status["qdrant"]="connected"
        except Exception as e: status["qdrant"] = f"error: {type(e).__name__}"
    if settings.neo4j_enabled:
        try:
            driver=_neo4j_driver(); driver.verify_connectivity(); status["neo4j"]="connected"
        except Exception as e: status["neo4j"] = f"error: {type(e).__name__}"
    if settings.google_calendar_enabled and Path(settings.google_calendar_token_file).exists(): status["google_calendar"]="token file present (runtime verification on use)"
    elif settings.google_calendar_enabled and Path(settings.google_calendar_credentials_file).exists(): status["google_calendar"]="credentials present; run OAuth setup"
    if _has_module("langgraph"): status["langgraph"]="installed; retrieval workflow enabled"
    if _has_module("mcp"): status["mcp"]="installed; run python -m app.mcp_server"
    return status


def _has_module(name: str) -> bool:
    try: __import__(name); return True
    except Exception: return False


_embed_model = None
_qdrant_client = None
_neo4j_driver_obj = None


def _embed(texts: list[str]) -> list[list[float]]:
    """The embedding model is loaded ONCE per process (it was reloaded for every message before)."""
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        _embed_model = TextEmbedding(model_name=settings.embedding_model)
    return [v.tolist() for v in _embed_model.embed(texts)]


def _qdrant():
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient
        _qdrant_client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None, timeout=15)
    return _qdrant_client


def _ensure_collection(client) -> None:
    from qdrant_client import models
    names = {c.name for c in client.get_collections().collections}
    if settings.qdrant_collection not in names:
        client.create_collection(collection_name=settings.qdrant_collection,
                                 vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE))
    try:  # tenant filter field; harmless if it already exists
        client.create_payload_index(settings.qdrant_collection, "owner_id", models.PayloadSchemaType.INTEGER)
    except Exception:
        pass


def _neo4j_driver():
    global _neo4j_driver_obj
    if _neo4j_driver_obj is None:
        from neo4j import GraphDatabase
        _neo4j_driver_obj = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    return _neo4j_driver_obj


def _payload(m: Any) -> dict[str, Any]:
    return {"message_id": m.id, "owner_id": m.owner_id, "team_id": m.team_id, "channel_id": m.channel_id, "text": m.text,
            "priority": m.priority_label, "author": m.author_name or m.user_id, "project_id": m.project_id,
            "created_at": m.message_created_at.isoformat() if m.message_created_at else None}


def index_messages(messages: list[Any]) -> dict[str, Any]:
    """Batch-index into Qdrant and Neo4j. Returns {"qdrant": status, "neo4j": status, "vector_ok": bool}."""
    out: dict[str, Any] = {"vector_ok": False}
    if not messages:
        return out
    if not settings.qdrant_enabled:
        out["qdrant"] = "skipped: QDRANT_URL not configured"
    else:
        try:
            from qdrant_client import models
            client = _qdrant(); _ensure_collection(client)
            vectors = _embed([m.text or "" for m in messages])
            client.upsert(collection_name=settings.qdrant_collection,
                          points=[models.PointStruct(id=m.id, vector=v, payload=_payload(m)) for m, v in zip(messages, vectors)])
            out["qdrant"], out["vector_ok"] = "indexed", True
        except Exception as exc:
            out["qdrant"] = f"error: {type(exc).__name__}: {exc}"
    if not settings.neo4j_enabled:
        out["neo4j"] = "skipped: NEO4J_URI not configured"
    else:
        try:
            rows = [{"id": m.id, "owner": m.owner_id, "team": m.team_id, "channel": f"{m.owner_id}:{m.channel_id}", "text": m.text,
                     "priority": m.priority_label, "created": m.message_created_at.isoformat() if m.message_created_at else None,
                     "author": f"{m.owner_id}:{m.team_id}:{m.user_id or 'unknown'}", "aname": m.author_name or m.user_id or "unknown",
                     "project": f"{m.owner_id}:{m.project_id}" if m.project_id else None} for m in messages]
            with _neo4j_driver().session(database=settings.neo4j_database) as session:
                session.run("""UNWIND $rows AS r
                  MERGE (w:Workspace {id: r.owner + ':' + r.team})
                  MERGE (c:Channel {id: r.channel}) MERGE (w)-[:HAS_CHANNEL]->(c)
                  MERGE (m:Message {id: r.id}) SET m.text = r.text, m.priority = r.priority, m.created_at = r.created, m.owner_id = r.owner
                  MERGE (c)-[:CONTAINS]->(m)
                  MERGE (u:User {id: r.author}) SET u.name = r.aname MERGE (u)-[:WROTE]->(m)
                  FOREACH (_ IN CASE WHEN r.project IS NULL THEN [] ELSE [1] END |
                    MERGE (p:Project {id: r.project}) MERGE (m)-[:ABOUT]->(p) MERGE (u)-[:PARTICIPATES_IN]->(p) MERGE (c)-[:DISCUSSES]->(p))""", rows=rows)
            out["neo4j"] = "synced"
        except Exception as exc:
            out["neo4j"] = f"error: {type(exc).__name__}: {exc}"
    return out


def delete_message_index(message_ids: list[int]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not message_ids:
        return out
    if settings.qdrant_enabled:
        try:
            from qdrant_client import models
            _qdrant().delete(collection_name=settings.qdrant_collection, points_selector=models.PointIdsList(points=message_ids))
            out["qdrant"] = "deleted"
        except Exception as exc:
            out["qdrant"] = f"error: {type(exc).__name__}"
    if settings.neo4j_enabled:
        try:
            with _neo4j_driver().session(database=settings.neo4j_database) as session:
                session.run("MATCH (m:Message) WHERE m.id IN $ids DETACH DELETE m", ids=message_ids)
            out["neo4j"] = "deleted"
        except Exception as exc:
            out["neo4j"] = f"error: {type(exc).__name__}"
    return out


def project_graph(owner_id: int, project_id: int) -> dict[str, Any] | None:
    """Who talks about a project and where: a real graph query. Returns None when Neo4j is off/unreachable (caller uses SQL)."""
    if not settings.neo4j_enabled:
        return None
    try:
        pid = f"{owner_id}:{project_id}"
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            people = session.run("""MATCH (u:User)-[:WROTE]->(m:Message)-[:ABOUT]->(:Project {id: $p})
                                    RETURN u.name AS name, count(m) AS n ORDER BY n DESC LIMIT 8""", p=pid).data()
            channels = session.run("""MATCH (c:Channel)-[:CONTAINS]->(m:Message)-[:ABOUT]->(:Project {id: $p})
                                      RETURN c.id AS channel, count(m) AS n ORDER BY n DESC LIMIT 6""", p=pid).data()
        return {"people": people, "channels": channels, "source": "neo4j"}
    except Exception:
        return None


# Single-message wrappers kept for backwards compatibility.
def upsert_message_vector(message: Any) -> str:
    return index_messages([message]).get("qdrant", "skipped")


def sync_message_graph(message: Any) -> str:
    return index_messages([message]).get("neo4j", "skipped")


def search_vectors(question: str, owner_id: int, limit: int = 5, project_id: int | None = None) -> list[dict[str, Any]]:
    """Semantic search restricted to ONE user's messages (tenant filter is mandatory)."""
    if not settings.qdrant_enabled:
        return []
    try:
        from qdrant_client import models
        client = _qdrant()
        if settings.qdrant_collection not in {c.name for c in client.get_collections().collections}:
            return []
        must = [models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id))]
        if project_id:
            must.append(models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)))
        flt = models.Filter(must=must)
        hits = client.query_points(collection_name=settings.qdrant_collection, query=_embed([question])[0], limit=limit,
                                   query_filter=flt, with_payload=True).points
        return [{"message_id": h.payload.get("message_id"), "channel_id": h.payload.get("channel_id"), "priority": h.payload.get("priority"),
                 "text": h.payload.get("text"), "score": h.score} for h in hits]
    except Exception:
        return []


def retrieve_with_langgraph(question: str, fallback_sources: list[dict[str, Any]], owner_id: int | None = None) -> dict[str, Any]:
    """Small LangGraph workflow: retrieve context, then return grounded snippets (no LLM key required)."""
    try:
        from typing_extensions import TypedDict
        from langgraph.graph import StateGraph, END
        class State(TypedDict):
            question: str
            sources: list[dict[str, Any]]
            answer: str
        def retrieve(state: State):
            sources=(search_vectors(state["question"], owner_id, 5) if owner_id is not None else []) or fallback_sources
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
