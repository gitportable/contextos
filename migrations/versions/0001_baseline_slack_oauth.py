"""Baseline: full schema incl. users, sessions, Slack connections, conversations, ownership columns.

Creates every table that does not exist yet from the current models (checkfirst). It does NOT alter tables from the
pre-OAuth MVP: those rows had no owner. For such a database run scripts/reset_dev_db.py first (dev), or write a
data migration that assigns an owner. Future schema changes: `alembic revision --autogenerate -m "..."`.
"""
from alembic import op

import app.models  # noqa: F401
from app.database import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("Baseline downgrade is intentionally unsupported.")
