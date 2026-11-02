"""Allow OAuth states without legacy Slack scopes."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns("oauth_states")}
    scopes = columns.get("scopes")
    if scopes is not None and not scopes["nullable"]:
        with op.batch_alter_table("oauth_states") as batch:
            batch.alter_column(
                "scopes",
                existing_type=scopes["type"],
                existing_nullable=False,
                nullable=True,
            )


def downgrade() -> None:
    raise RuntimeError("Downgrade is intentionally unsupported; restore from backup.")
