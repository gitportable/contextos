"""Google sign-in, permission levels, projects + summaries, LLM usage, pipeline events, retention.

Idempotent on purpose: 0001 builds the *current* models on an empty database, so a fresh install already has everything
here and this revision only fills gaps. On an older database it adds the new columns/tables without touching existing rows.
"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401
from app.database import Base

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# (table, column, type, nullable, server_default)
ADD = [
    ("users", "google_sub", sa.String(64), True, None),
    ("users", "email_verified", sa.Boolean(), False, sa.false()),
    ("users", "avatar_url", sa.String(500), True, None),
    ("users", "projects_discovered_at", sa.DateTime(timezone=True), True, None),
    ("oauth_states", "purpose", sa.String(30), False, "slack_connect"),
    ("oauth_states", "binder_hash", sa.String(64), True, None),
    ("oauth_states", "nonce", sa.String(100), True, None),
    ("oauth_states", "code_verifier", sa.String(200), True, None),
    ("oauth_states", "payload", sa.String(200), True, None),
    ("slack_connections", "access_level", sa.String(20), False, "recommended"),
    ("slack_messages", "compacted_at", sa.DateTime(timezone=True), True, None),
    ("processing_jobs", "owner_id", sa.Integer(), True, None),
    ("slack_connections", "provider", sa.String(20), False, "slack"),
]
INDEXES = [("ix_users_google_sub", "users", ["google_sub"], True), ("ix_processing_jobs_owner_id", "processing_jobs", ["owner_id"], False)]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)  # all new tables
    insp = sa.inspect(bind)
    for table, col, typ, nullable, default in ADD:
        if not insp.has_table(table) or col in {c["name"] for c in insp.get_columns(table)}:
            continue
        op.add_column(table, sa.Column(col, typ, nullable=nullable, server_default=default))
    insp = sa.inspect(bind)
    for name, table, cols, unique in INDEXES:
        if insp.has_table(table) and name not in {i["name"] for i in insp.get_indexes(table)}:
            op.create_index(name, table, cols, unique=unique)
    # Google sign-in states have no user yet.
    if any(c["name"] == "user_id" and not c["nullable"] for c in insp.get_columns("oauth_states")):
        with op.batch_alter_table("oauth_states") as batch:
            batch.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
    # Connections made before permission levels existed: reflect what was actually granted.
    op.execute(sa.text("UPDATE slack_connections SET access_level='with_replies' WHERE granted_user_scopes LIKE '%chat:write%'"))


def downgrade() -> None:
    raise RuntimeError("Downgrade is intentionally unsupported; restore from backup.")
