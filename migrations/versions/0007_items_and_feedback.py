"""Add obligation items, ranking feedback events, and message action timestamps."""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401
from app.database import Base

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    insp = sa.inspect(bind)
    message_columns = {column["name"] for column in insp.get_columns("slack_messages")}
    for name in ("handled_at", "snoozed_until"):
        if name not in message_columns:
            op.add_column("slack_messages", sa.Column(name, sa.DateTime(timezone=True), nullable=True))

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("slack_messages")}
    for name, column in (
        ("ix_slack_messages_handled_at", "handled_at"),
        ("ix_slack_messages_snoozed_until", "snoozed_until"),
    ):
        if name not in indexes:
            op.create_index(name, "slack_messages", [column])


def downgrade() -> None:
    raise RuntimeError("Downgrade is intentionally unsupported; restore from backup.")
