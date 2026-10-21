"""DEV ONLY. Drops every ContextOS table (including the pre-OAuth ones) and recreates the current schema.
Old rows had no owner, so they cannot be migrated safely. Usage: python scripts/reset_dev_db.py --yes"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import MetaData  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, engine  # noqa: E402
import app.models  # noqa: E402,F401

if "--yes" not in sys.argv:
    raise SystemExit(f"This DROPS ALL TABLES in {engine.url.render_as_string(hide_password=True)}.\nRe-run with --yes to confirm.")
if settings.app_env != "development":
    raise SystemExit("Refusing: APP_ENV is not 'development'.")
meta = MetaData(); meta.reflect(bind=engine); meta.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
print("Database reset. Current schema created.")
