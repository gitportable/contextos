"""Store per-user, encrypted Google Calendar OAuth credentials."""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("google_calendar_connections"):
        op.create_table(
            "google_calendar_connections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("google_sub", sa.String(length=64), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=False),
            sa.Column("access_token_encrypted", sa.Text(), nullable=False),
            sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
            sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    insp = sa.inspect(bind)
    if "ix_google_calendar_connections_owner_id" not in {i["name"] for i in insp.get_indexes("google_calendar_connections")}:
        op.create_index("ix_google_calendar_connections_owner_id", "google_calendar_connections", ["owner_id"], unique=True)
    if "ix_google_calendar_connections_status" not in {i["name"] for i in insp.get_indexes("google_calendar_connections")}:
        op.create_index("ix_google_calendar_connections_status", "google_calendar_connections", ["status"])


def downgrade() -> None:
    raise RuntimeError("Downgrade is intentionally unsupported; restore from backup.")
