"""Optional integrations: Qdrant (semantic search), Neo4j (relationship queries), Google Calendar.
Each adapter fails soft: when it is not configured, or down, ContextOS keeps working from PostgreSQL alone.

Tenant isolation: EVERY Qdrant query filters on owner_id and EVERY Cypher query constrains the owner, and callers re-load
rows from PostgreSQL filtered by owner before showing anything, so even a corrupted index cannot leak another user's text.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app import cache
from app.config import settings

log = logging.getLogger("contextos.integrations")
SUMMARY_POINT_BASE = 10 ** 12   # message points use the message id; project-summary points live above this


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def integration_status() -> dict[str, Any]:
    status = {"postgres": "managed by API", "slack_events": "configured" if settings.slack_signing_secret else "needs signing secret",
              "slack_oauth": "configured" if settings.slack_oauth_configured else "needs SLACK_CLIENT_ID/SECRET",
              "google_login": "configured" if settings.google_oauth_configured else "needs GOOGLE_CLIENT_ID/SECRET",
              "token_encryption": "configured" if settings.token_encryption_key else "needs TOKEN_ENCRYPTION_KEY",
              "llm": f"configured ({settings.llm_model})" if settings.llm_enabled else "not configured (extractive fallback)",
              "redis": "not configured", "qdrant": "not configured", "neo4j": "not configured",
              "mcp": "installed; run python -m app.mcp_server" if _has_module("mcp") else "install dependency",
              "google_calendar": "not configured"}
    if settings.redis_url:
        status["redis"] = "connected" if cache.client() else "unreachable (rate limits fall back to per-process memory)"
    if settings.qdrant_enabled:
        try:
            _qdrant().get_collections()
            status["qdrant"] = "connected"
        except Exception as e:
            status["qdrant"] = f"error: {type(e).__name__}"
    if settings.neo4j_enabled:
        try:
            _neo4j_driver().verify_connectivity()
            status["neo4j"] = "connected"
        except Exception as e:
            status["neo4j"] = f"error: {type(e).__name__}"
    if settings.google_calendar_enabled and Path(settings.google_calendar_token_file).exists():
        status["google_calendar"] = "token file present (single-user; per-user calendar OAuth is future work)"
    elif settings.google_calendar_enabled and Path(settings.google_calendar_credentials_file).exists():
        status["google_calendar"] = "credentials present; run OAuth setup"
    return status


_embed_model = None
_qdrant_client = None
_neo4j_driver_obj = None


def _embed(texts: list[str]) -> list[list[float]]:
    """The embedding model is loaded ONCE per process."""
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


_collection_ready = False


def _ensure_collection(client) -> None:
    global _collection_ready
    if _collection_ready:
        return
    from qdrant_client import models
    names = {c.name for c in client.get_collections().collections}
    if settings.qdrant_collection not in names:
        client.create_collection(collection_name=settings.qdrant_collection,
                                 vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE))
    for field, schema in (("owner_id", models.PayloadSchemaType.INTEGER), ("kind", models.PayloadSchemaType.KEYWORD),
                          ("priority", models.PayloadSchemaType.KEYWORD), ("project_ids", models.PayloadSchemaType.INTEGER)):
        try:  # payload indexes make the mandatory tenant filter fast; harmless if they already exist
            client.create_payload_index(settings.qdrant_collection, field, schema)
        except Exception:
            pass
    _collection_ready = True


def _neo4j_driver():
    global _neo4j_driver_obj
    if _neo4j_driver_obj is None:
        from neo4j import GraphDatabase
        _neo4j_driver_obj = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    return _neo4j_driver_obj


_graph_ready = False


def _ensure_graph_schema(session) -> None:
    """Uniqueness constraints (they also create the indexes MERGE needs: without them every MERGE scans all nodes)."""
    global _graph_ready
    if _graph_ready:
        return
    for label in ("Message", "Person", "Channel", "Workspace", "Project"):
        try:
            session.run(f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE")
        except Exception as exc:  # older server / insufficient rights: slower, but correct
            log.warning("neo4j_constraint_skipped %s %s", label, type(exc).__name__)
    _graph_ready = True


def _payload(m: Any) -> dict[str, Any]:
    return {"kind": "message", "message_id": m.id, "owner_id": m.owner_id, "team_id": m.team_id, "channel_id": m.channel_id, "text": m.text,
            "priority": m.priority_label, "author": m.author_name or m.user_id, "project_ids": [],
            "created_at": m.message_created_at.isoformat() if m.message_created_at else None}


def _graph_rows(messages: list[Any]) -> list[dict]:
    return [{"id": m.id, "owner": m.owner_id, "team": m.team_id, "channel": f"{m.owner_id}:{m.channel_id}", "priority": m.priority_label,
             "created": m.message_created_at.isoformat() if m.message_created_at else None,
             "person": f"{m.owner_id}:{m.team_id}:{m.user_id or 'unknown'}", "name": m.author_name or m.user_id or "unknown",
             "is_bot": bool(m.is_bot)} for m in messages]


def index_vectors(messages: list[Any]) -> str:
    """Embed + upsert into Qdrant. Returns a status string: indexed | skipped: ... | error: ..."""
    if not settings.qdrant_enabled:
        return "skipped: QDRANT_URL not configured"
    try:
        from qdrant_client import models
        client = _qdrant()
        _ensure_collection(client)
        vectors = _embed([m.text or "" for m in messages])
        client.upsert(collection_name=settings.qdrant_collection,
                      points=[models.PointStruct(id=m.id, vector=v, payload=_payload(m)) for m, v in zip(messages, vectors)])
        return "indexed"
    except Exception as exc:
        log.warning("qdrant_index_error %s %s", type(exc).__name__, str(exc)[:200])
        return f"error: {type(exc).__name__}"


def index_graph(messages: list[Any]) -> str:
    """MERGE people / channels / messages into Neo4j (idempotent, so it is safe to run again for the same messages)."""
    if not settings.neo4j_enabled:
        return "skipped: NEO4J_URI not configured"
    try:
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            _ensure_graph_schema(session)
            session.run("""UNWIND $rows AS r
              MERGE (w:Workspace {id: toString(r.owner) + ':' + r.team}) SET w.owner = r.owner
              MERGE (c:Channel {id: r.channel}) SET c.owner = r.owner
              MERGE (w)-[:HAS_CHANNEL]->(c)
              MERGE (m:Message {id: r.id}) SET m.priority = r.priority, m.created_at = r.created, m.owner_id = r.owner
              MERGE (c)-[:CONTAINS]->(m)
              MERGE (u:Person {id: r.person}) SET u.name = r.name, u.owner = r.owner, u.is_bot = r.is_bot
              MERGE (u)-[:WROTE]->(m)""", rows=_graph_rows(messages))
        return "synced"
    except Exception as exc:
        log.warning("neo4j_index_error %s %s", type(exc).__name__, str(exc)[:200])
        return f"error: {type(exc).__name__}"


def index_messages(messages: list[Any]) -> dict[str, Any]:
    """Batch-index into Qdrant and Neo4j. Returns {"qdrant": status, "neo4j": status, "vector_ok": bool}."""
    out: dict[str, Any] = {"vector_ok": False}
    if not messages:
        return out
    out["qdrant"] = index_vectors(messages)
    out["vector_ok"] = out["qdrant"] == "indexed"
    out["neo4j"] = index_graph(messages)
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


# ------------------------------------------------------------ qdrant: projects ---
def set_message_projects(owner_id: int, links: dict[int, list[int]]) -> str:
    """Write project_ids onto message points so semantic search can be restricted to a project. links: message_id -> [project ids]."""
    if not settings.qdrant_enabled or not links:
        return "skipped"
    try:
        client = _qdrant()
        groups: dict[tuple, list[int]] = {}
        for mid, pids in links.items():
            groups.setdefault(tuple(sorted(pids)), []).append(mid)
        for pids, mids in groups.items():
            for i in range(0, len(mids), 500):
                client.set_payload(collection_name=settings.qdrant_collection, payload={"project_ids": list(pids)}, points=mids[i:i + 500])
        return "ok"
    except Exception as exc:
        log.warning("qdrant_set_projects_error %s", type(exc).__name__)
        return f"error: {type(exc).__name__}"


def index_summary(owner_id: int, project_id: int, name: str, text: str) -> str:
    """One point per project (replaced on every new summary version) so the assistant can retrieve summaries semantically."""
    if not settings.qdrant_enabled or not text:
        return "skipped"
    try:
        from qdrant_client import models
        client = _qdrant()
        _ensure_collection(client)
        vec = _embed([f"{name}: {text}"])[0]
        client.upsert(collection_name=settings.qdrant_collection, points=[models.PointStruct(
            id=SUMMARY_POINT_BASE + project_id, vector=vec,
            payload={"kind": "summary", "owner_id": owner_id, "project_ids": [project_id], "project_id": project_id, "name": name, "text": text[:2000]})])
        return "indexed"
    except Exception as exc:
        log.warning("qdrant_summary_error %s", type(exc).__name__)
        return f"error: {type(exc).__name__}"


def delete_summary_index(project_ids: list[int]) -> None:
    if not settings.qdrant_enabled or not project_ids:
        return
    try:
        from qdrant_client import models
        _qdrant().delete(collection_name=settings.qdrant_collection,
                         points_selector=models.PointIdsList(points=[SUMMARY_POINT_BASE + p for p in project_ids]))
    except Exception:
        pass


def search_vectors(question: str, owner_id: int, limit: int = 5, project_ids: list[int] | None = None,
                   kind: str = "message", exclude_ids: list[int] | None = None, min_score: float | None = None) -> list[dict[str, Any]]:
    """Semantic search restricted to ONE user's points (tenant filter is mandatory), optionally to given projects."""
    if not settings.qdrant_enabled:
        return []
    try:
        from qdrant_client import models
        client = _qdrant()
        if settings.qdrant_collection not in {c.name for c in client.get_collections().collections}:
            return []
        must = [models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id)),
                models.FieldCondition(key="kind", match=models.MatchValue(value=kind))]
        if project_ids:
            must.append(models.FieldCondition(key="project_ids", match=models.MatchAny(any=list(project_ids))))
        must_not = [models.HasIdCondition(has_id=list(exclude_ids))] if exclude_ids else None
        hits = client.query_points(collection_name=settings.qdrant_collection, query=_embed([question])[0], limit=limit,
                                   query_filter=models.Filter(must=must, must_not=must_not), with_payload=True,
                                   score_threshold=min_score).points
        return [{"message_id": h.payload.get("message_id"), "project_id": h.payload.get("project_id"), "channel_id": h.payload.get("channel_id"),
                 "priority": h.payload.get("priority"), "text": h.payload.get("text"), "score": h.score} for h in hits]
    except Exception:
        return []


def similar_project_pairs(owner_id: int, min_score: float) -> list[tuple[int, int, float]] | None:
    """Pairs of the user's projects whose SUMMARY vectors are close: 'these look like the same thing'. None = Qdrant unavailable."""
    if not settings.qdrant_enabled:
        return None
    try:
        from qdrant_client import models
        client = _qdrant()
        if settings.qdrant_collection not in {c.name for c in client.get_collections().collections}:
            return None
        flt = models.Filter(must=[models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id)),
                                  models.FieldCondition(key="kind", match=models.MatchValue(value="summary"))])
        pts, _ = client.scroll(collection_name=settings.qdrant_collection, scroll_filter=flt, limit=200, with_payload=True, with_vectors=True)
        vecs = [(int(p.payload["project_id"]), p.vector) for p in pts if p.payload.get("project_id") and p.vector is not None]
        out = []
        for i in range(len(vecs)):
            for j in range(i + 1, len(vecs)):
                a, b = vecs[i][1], vecs[j][1]
                dot = sum(x * y for x, y in zip(a, b))
                na, nb = sum(x * x for x in a) ** 0.5, sum(y * y for y in b) ** 0.5
                score = dot / (na * nb) if na and nb else 0.0
                if score >= min_score:
                    out.append((vecs[i][0], vecs[j][0], round(score, 3)))
        return sorted(out, key=lambda t: -t[2])
    except Exception as exc:
        log.warning("qdrant_similar_projects_error %s", type(exc).__name__)
        return None


def nearest_urgent(texts: list[str], owner_id: int, min_score: float) -> list[tuple[int, int, float]] | None:
    """For each text, the closest P0/P1 message of this user: [(text_index, message_id, score)] for matches >= min_score.
    None = Qdrant unavailable (the caller skips; nothing is lost)."""
    if not settings.qdrant_enabled or not texts:
        return None
    try:
        from qdrant_client import models
        client = _qdrant()
        if settings.qdrant_collection not in {c.name for c in client.get_collections().collections}:
            return None
        flt = models.Filter(must=[models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id)),
                                  models.FieldCondition(key="kind", match=models.MatchValue(value="message")),
                                  models.FieldCondition(key="priority", match=models.MatchAny(any=["P0", "P1"]))])
        out = []
        for i, vec in enumerate(_embed(texts)):
            hits = client.query_points(collection_name=settings.qdrant_collection, query=vec, limit=1, query_filter=flt, with_payload=True,
                                       score_threshold=min_score).points
            if hits and hits[0].payload.get("message_id"):
                out.append((i, int(hits[0].payload["message_id"]), float(hits[0].score)))
        return out
    except Exception as exc:
        log.warning("qdrant_urgent_error %s", type(exc).__name__)
        return None


# -------------------------------------------------------------- neo4j: graph ---
def sync_project_graph(owner_id: int, projects: list[dict], links: dict[int, list[int]]) -> str:
    """Mirror projects and message->project links into the graph (replaces this owner's links, never anyone else's).
    projects: [{id, name}], links: message_id -> [project ids]."""
    if not settings.neo4j_enabled:
        return "skipped"
    try:
        pairs = [{"m": mid, "p": pid} for mid, pids in links.items() for pid in pids]
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            session.run("MATCH (:Message {owner_id: $owner})-[r:ABOUT]->(:Project {owner: $owner}) DELETE r", owner=owner_id)
            session.run("""UNWIND $projects AS p
                MERGE (x:Project {id: p.id}) SET x.name = p.name, x.owner = $owner""", projects=projects, owner=owner_id)
            for i in range(0, len(pairs), 2000):
                session.run("""UNWIND $pairs AS r
                    MATCH (m:Message {id: r.m, owner_id: $owner}) MATCH (p:Project {id: r.p, owner: $owner})
                    MERGE (m)-[:ABOUT]->(p)""", pairs=pairs[i:i + 2000], owner=owner_id)
        return "synced"
    except Exception as exc:
        log.warning("neo4j_project_sync_error %s", type(exc).__name__)
        return f"error: {type(exc).__name__}"


def graph_project_people(owner_id: int, project_id: int, limit: int = 8) -> list[dict] | None:
    """'Who is involved in project X?' None means the graph is unavailable (caller falls back to PostgreSQL)."""
    if not settings.neo4j_enabled:
        return None
    try:
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            res = session.run("""MATCH (p:Project {id: $pid, owner: $owner})<-[:ABOUT]-(m:Message {owner_id: $owner})<-[:WROTE]-(u:Person {owner: $owner})
                WHERE coalesce(u.is_bot, false) = false
                RETURN u.name AS name, count(m) AS messages, max(m.created_at) AS last_active
                ORDER BY messages DESC LIMIT $limit""", pid=project_id, owner=owner_id, limit=limit)
            rows = [{"name": r["name"], "messages": r["messages"], "last_active": r["last_active"]} for r in res]
        return rows or None
    except Exception:
        return None


def graph_related_projects(owner_id: int, project_id: int, limit: int = 5) -> list[dict] | None:
    """'Which other projects share people with this one?' (cross-project load / dependency hint)."""
    if not settings.neo4j_enabled:
        return None
    try:
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            res = session.run("""MATCH (p:Project {id: $pid, owner: $owner})<-[:ABOUT]-(:Message {owner_id: $owner})<-[:WROTE]-(u:Person {owner: $owner})
                -[:WROTE]->(:Message {owner_id: $owner})-[:ABOUT]->(q:Project {owner: $owner})
                WHERE q.id <> p.id AND coalesce(u.is_bot, false) = false
                RETURN q.id AS id, q.name AS name, count(DISTINCT u) AS shared_people ORDER BY shared_people DESC LIMIT $limit""",
                              pid=project_id, owner=owner_id, limit=limit)
            return [{"id": r["id"], "name": r["name"], "shared_people": r["shared_people"]} for r in res] or None
    except Exception:
        return None


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


# ------------------------------------------------- derived intelligence ---
def nearest_projects(texts: list[str], owner_id: int, min_score: float) -> list[tuple[int, int, float]] | None:
    """For each text, the project whose SUMMARY is semantically closest (tenant-filtered). Returns [(text_index, project_id, score)]
    for matches >= min_score, or None when Qdrant is unavailable (caller skips; nothing is lost)."""
    if not settings.qdrant_enabled or not texts:
        return None
    try:
        from qdrant_client import models
        client = _qdrant()
        if settings.qdrant_collection not in {c.name for c in client.get_collections().collections}:
            return None
        flt = models.Filter(must=[models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id)),
                                  models.FieldCondition(key="kind", match=models.MatchValue(value="summary"))])
        out = []
        vectors = _embed(texts)
        for i, vec in enumerate(vectors):
            hits = client.query_points(collection_name=settings.qdrant_collection, query=vec, limit=1, query_filter=flt, with_payload=True).points
            if hits and hits[0].score >= min_score and hits[0].payload.get("project_id"):
                out.append((i, int(hits[0].payload["project_id"]), float(hits[0].score)))
        return out
    except Exception as exc:
        log.warning("qdrant_nearest_error %s", type(exc).__name__)
        return None


def qdrant_stats(owner_id: int) -> dict | None:
    if not settings.qdrant_enabled:
        return None
    try:
        from qdrant_client import models
        client = _qdrant()
        if settings.qdrant_collection not in {c.name for c in client.get_collections().collections}:
            return {"messages": 0, "summaries": 0}

        def n(kind):
            return client.count(collection_name=settings.qdrant_collection, exact=True, count_filter=models.Filter(must=[
                models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id)),
                models.FieldCondition(key="kind", match=models.MatchValue(value=kind))])).count
        return {"messages": n("message"), "summaries": n("summary")}
    except Exception as exc:
        return {"error": type(exc).__name__}


def neo4j_stats(owner_id: int) -> dict | None:
    if not settings.neo4j_enabled:
        return None
    try:
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            r = session.run("""CALL { MATCH (m:Message {owner_id: $o}) RETURN count(m) AS messages }
                CALL { MATCH (p:Project {owner: $o}) RETURN count(p) AS projects }
                CALL { MATCH (u:Person {owner: $o}) RETURN count(u) AS people }
                CALL { MATCH (:Message {owner_id: $o})-[a:ABOUT]->(:Project {owner: $o}) RETURN count(a) AS links }
                RETURN messages, projects, people, links""", o=owner_id).single()
            return dict(r) if r else {"messages": 0, "projects": 0, "people": 0, "links": 0}
    except Exception as exc:
        return {"error": type(exc).__name__}


def graph_key_people(owner_id: int, min_projects: int, min_messages: int, limit: int = 40) -> list[dict] | None:
    """People who write about several of the user's projects: the 'connectors'. None = graph unavailable."""
    if not settings.neo4j_enabled:
        return None
    try:
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            res = session.run("""MATCH (u:Person {owner: $owner})-[:WROTE]->(m:Message {owner_id: $owner})-[:ABOUT]->(p:Project {owner: $owner})
                WHERE coalesce(u.is_bot, false) = false
                WITH u, count(DISTINCT p) AS projects, count(m) AS messages
                WHERE projects >= $minp AND messages >= $minm
                RETURN u.name AS name, projects, messages ORDER BY projects DESC, messages DESC LIMIT $limit""",
                              owner=owner_id, minp=min_projects, minm=min_messages, limit=limit)
            return [{"name": r["name"], "projects": r["projects"], "messages": r["messages"]} for r in res]
    except Exception:
        return None


# ------------------------------------------------------- neo4j: people & risk ---
def graph_people(owner_id: int, limit: int = 60) -> list[dict] | None:
    """Everyone who wrote in the user's messages, with how many projects they touch. None = graph unavailable."""
    if not settings.neo4j_enabled:
        return None
    try:
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            res = session.run("""MATCH (u:Person {owner: $owner}) WHERE coalesce(u.is_bot, false) = false
                OPTIONAL MATCH (u)-[:WROTE]->(m:Message {owner_id: $owner})
                OPTIONAL MATCH (m)-[:ABOUT]->(p:Project {owner: $owner})
                RETURN u.name AS name, count(DISTINCT m) AS messages, count(DISTINCT p) AS projects, max(m.created_at) AS last_active
                ORDER BY projects DESC, messages DESC LIMIT $limit""", owner=owner_id, limit=limit)
            return [{"name": r["name"], "messages": r["messages"], "projects": r["projects"], "last_active": r["last_active"]} for r in res]
    except Exception:
        return None


def graph_person_projects(owner_id: int, name: str, limit: int = 12) -> list[dict] | None:
    """The projects one person (matched by display name, so Slack + Gmail count once) writes about."""
    if not settings.neo4j_enabled:
        return None
    try:
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            res = session.run("""MATCH (u:Person {owner: $owner})-[:WROTE]->(m:Message {owner_id: $owner})-[:ABOUT]->(p:Project {owner: $owner})
                WHERE toLower(u.name) = $name
                RETURN p.id AS id, p.name AS name, count(m) AS messages ORDER BY messages DESC LIMIT $limit""",
                              owner=owner_id, name=name.strip().lower(), limit=limit)
            return [{"id": r["id"], "name": r["name"], "messages": r["messages"]} for r in res]
    except Exception:
        return None


def graph_works_with(owner_id: int, name: str, limit: int = 8) -> list[dict] | None:
    """Who does this person share projects with, ranked by number of shared projects (Person-WROTE->Message-ABOUT->Project<-ABOUT-Message<-WROTE-Person)."""
    if not settings.neo4j_enabled:
        return None
    try:
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            res = session.run("""MATCH (u:Person {owner: $owner})-[:WROTE]->(:Message {owner_id: $owner})-[:ABOUT]->(p:Project {owner: $owner})
                      <-[:ABOUT]-(:Message {owner_id: $owner})<-[:WROTE]-(v:Person {owner: $owner})
                WHERE toLower(u.name) = $name AND toLower(v.name) <> $name AND coalesce(v.is_bot, false) = false
                RETURN v.name AS name, count(DISTINCT p) AS shared_projects ORDER BY shared_projects DESC LIMIT $limit""",
                              owner=owner_id, name=name.strip().lower(), limit=limit)
            return [{"name": r["name"], "shared_projects": r["shared_projects"]} for r in res]
    except Exception:
        return None


def graph_project_authors(owner_id: int) -> list[dict] | None:
    """Per project: total messages and each person's share (feeds the 'only one person knows this' warning)."""
    if not settings.neo4j_enabled:
        return None
    try:
        with _neo4j_driver().session(database=settings.neo4j_database) as session:
            res = session.run("""MATCH (u:Person {owner: $owner})-[:WROTE]->(m:Message {owner_id: $owner})-[:ABOUT]->(p:Project {owner: $owner})
                WHERE coalesce(u.is_bot, false) = false
                WITH p, toLower(u.name) AS who, max(u.name) AS name, count(m) AS n
                RETURN p.id AS id, p.name AS project, collect({name: name, n: n}) AS authors""", owner=owner_id)
            return [{"id": r["id"], "project": r["project"], "authors": list(r["authors"])} for r in res]
    except Exception:
        return None
