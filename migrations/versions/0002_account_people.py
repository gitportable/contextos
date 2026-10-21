"""Add account directory metadata and assignment fields."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    additions = {
        "slack_connections": [sa.Column("people_synced_at", sa.DateTime(timezone=True), nullable=True)],
        "slack_users": [
            sa.Column("username", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("email", sa.String(length=320), nullable=True),
        ],
        "reminders": [sa.Column("assigned_people", sa.Text(), nullable=False, server_default="[]")],
        "suggested_events": [sa.Column("assigned_people", sa.Text(), nullable=False, server_default="[]")],
    }
    for table, columns in additions.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)


def downgrade() -> None:
    op.drop_column("suggested_events", "assigned_people")
    op.drop_column("reminders", "assigned_people")
    op.drop_column("slack_users", "email")
    op.drop_column("slack_users", "username")
    op.drop_column("slack_connections", "people_synced_at")
