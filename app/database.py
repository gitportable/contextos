import logging

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

log = logging.getLogger("contextos.db")
if settings.database_url.startswith("sqlite"):
    _kw = {}
elif settings.app_env == "production":
    _kw = {"pool_size": 1, "max_overflow": 0, "pool_recycle": 1800, "pool_timeout": 15}
else:
    _kw = {"pool_size": 10, "max_overflow": 20, "pool_recycle": 1800, "pool_timeout": 15}
engine = create_engine(settings.database_url, pool_pre_ping=True, **_kw)  # bounded pool: a traffic spike queues instead of exhausting Postgres
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
