"""Fill Qdrant and Neo4j from what PostgreSQL already holds (run after switching them on, or after wiping a volume).

    python scripts/backfill_indexes.py                       # every user
    python scripts/backfill_indexes.py --email you@x.com     # one user
    python scripts/backfill_indexes.py --force-graph         # rebuild the whole Neo4j graph even if counts match

Safe to run repeatedly: vectors are upserted and graph nodes are MERGEd. The worker also does this automatically (hourly, per user).
The first run downloads the embedding model (EMBEDDING_MODEL), so it needs internet once.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import integrations, intelligence  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email")
    ap.add_argument("--force-graph", action="store_true")
    args = ap.parse_args()
    if not (settings.qdrant_enabled or settings.neo4j_enabled):
        print("Neither QDRANT_URL nor NEO4J_URI is set in .env. Nothing to do.")
        return 1
    status = integrations.integration_status()
    print("qdrant:", status["qdrant"], "| neo4j:", status["neo4j"])
    with SessionLocal() as db:
        q = db.query(User).filter(User.is_active.is_(True))
        if args.email:
            q = q.filter(User.email == args.email.strip().lower())
        users = q.all()
        if not users:
            print("No matching users.")
            return 1
        for u in users:
            res = intelligence.backfill(db, u.id, force_graph=args.force_graph)
            print(f"{u.email}: {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
