"""Add model columns missing from databases created before the current schema."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add_column(table: str, column: sa.Column) -> None:
    if table in sa.inspect(op.get_bind()).get_table_names() and column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "pipeline_events" in tables:
        _add_column("pipeline_events", sa.Column("trace_id", sa.String(200), nullable=True))
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("pipeline_events")}
        if "ix_pipeline_events_trace_id" not in indexes:
            op.create_index("ix_pipeline_events_trace_id", "pipeline_events", ["trace_id"])

    if "worker_heartbeats" in tables:
        columns = _columns("worker_heartbeats")
        if "worker_id" not in columns:
            if "name" not in columns:
                raise RuntimeError("Cannot migrate worker_heartbeats: expected worker_id or legacy name column.")
            op.alter_column(
                "worker_heartbeats", "name", new_column_name="worker_id", existing_type=sa.String(80)
            )

        columns = _columns("worker_heartbeats")
        if "beat_at" not in columns:
            if "last_seen" not in columns:
                raise RuntimeError("Cannot migrate worker_heartbeats: expected beat_at or legacy last_seen column.")
            op.alter_column(
                "worker_heartbeats", "last_seen", new_column_name="beat_at", existing_type=sa.DateTime()
            )

    project_columns = (
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("channel_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("author_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_dirty", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_summary_at", sa.DateTime(timezone=True), nullable=True),
    )
    if "projects" in tables:
        for column in project_columns:
            _add_column("projects", column)

        columns = _columns("projects")
        if "last_activity_at" in columns:
            op.execute(sa.text("UPDATE projects SET last_seen_at = last_activity_at WHERE last_seen_at IS NULL"))
        if "summary_at" in columns:
            op.execute(sa.text("UPDATE projects SET last_summary_at = summary_at WHERE last_summary_at IS NULL"))


def downgrade() -> None:
    raise RuntimeError("Downgrade is intentionally unsupported; restore from backup.")
