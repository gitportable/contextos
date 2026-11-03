import logging

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

log = logging.getLogger("contextos.db")
def _engine_options() -> dict:
    """Hosted Postgres (Supabase etc.) drops idle connections and caps how many can be open: recycle early, stay small,
    and fail fast instead of hanging forever when the host is unreachable."""
    opts: dict = {"pool_pre_ping": True}
    if settings.database_url.startswith("postgresql"):
        opts.update(pool_size=5, max_overflow=2, pool_recycle=180, pool_timeout=5,
                    connect_args={"connect_timeout": 5, "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3})
    return opts


engine = create_engine(settings.database_url, **_engine_options())
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


def wait_for_database(attempts: int = 5, delay: float = 2.0) -> None:
    """Fail with one readable line (no stack trace) when the database cannot be reached or the password is wrong."""
    import time

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    last = None
    for i in range(attempts):
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            last = str(getattr(exc, "orig", exc)).strip().splitlines()[0][:300]
            log.warning("database_unreachable attempt=%s/%s %s", i + 1, attempts, last)
            time.sleep(delay)
    hint = ""
    low = (last or "").lower()
    if "password authentication failed" in low:
        hint = " The password in DATABASE_URL is wrong (URL-encode special characters, e.g. @ as %40)."
    elif "unreachable" in low or "could not translate" in low or "timeout" in low:
        hint = " If this is Supabase, use the Session pooler connection string instead of the direct db.<ref>.supabase.co host."
    raise RuntimeError(f"Cannot connect to the database: {last}.{hint}")


def init_db() -> None:
    """Create tables in development. Refuses to run against the pre-OAuth schema, which create_all cannot upgrade."""
    import app.models  # noqa: F401  (register tables)

    wait_for_database()
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
