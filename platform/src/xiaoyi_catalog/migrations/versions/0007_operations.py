"""Operational heartbeat and immutable migration provenance."""
from alembic import op
import sqlalchemy as sa
revision = "0007_operations"
down_revision = "0006_catalog_qa"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("worker_heartbeat",
        sa.Column("owner", sa.String(64), primary_key=True),
        sa.Column("queue", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)))
    op.create_index("ix_worker_heartbeat_queue", "worker_heartbeat", ["queue"])
    op.create_table("migration_record",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("source_revision", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("checkpoint_archive", sa.Text(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("migrated_run",
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_run.id"), primary_key=True),
        sa.Column("migration_id", sa.String(64), sa.ForeignKey("migration_record.id"), nullable=False),
        sa.Column("checkpoint_policy", sa.String(32), nullable=False))


def downgrade():
    raise RuntimeError("Restore the full pre-release backup to roll back migration provenance")
