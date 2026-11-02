import logging

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

log = logging.getLogger("contextos.db")
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class LegacySchemaError(RuntimeError):
    pass


def schema_drift() -> list[str]:
    """Columns the models expect but the database lacks (create_all never adds columns to existing tables)."""
    import app.models  # noqa: F401

    insp = inspect(engine)
    out = []
    for table in Base.metadata.sorted_tables:
        if insp.has_table(table.name):
            have = {c["name"] for c in insp.get_columns(table.name)}
            out += [f"{table.name}.{c.name}" for c in table.columns if c.name not in have]
    return out


def init_db() -> None:
    """Create tables in development. Refuses to run against the pre-OAuth schema, which create_all cannot upgrade."""
    import app.models  # noqa: F401  (register tables)

    insp = inspect(engine)
    if insp.has_table("slack_messages"):
        cols = {c["name"] for c in insp.get_columns("slack_messages")}
        if "connection_id" not in cols:
            raise LegacySchemaError(
                "Your database still has the pre-OAuth schema (slack_messages has no connection_id). "
                "Those rows have no owner, so they cannot be migrated safely. For a dev database run "
                "`python scripts/reset_dev_db.py --yes` once, then start the app again."
            )
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)
    missing = schema_drift()
    if missing:
        raise LegacySchemaError(
            "The database is older than this code (missing: " + ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "") + "). "
            "Run `alembic upgrade head` once, then start the app again."
        )
