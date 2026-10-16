"""Persistent metadata collection jobs, schedules and progress events."""
from alembic import op
import sqlalchemy as sa
revision = "0005_collection_jobs"
down_revision = "0004_derived_versions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("collection_schedule",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False), sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)), sa.Column("last_job_id", sa.Integer()),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_collection_schedule_enabled", "collection_schedule", ["enabled"])
    op.create_index("ix_collection_schedule_next_run_at", "collection_schedule", ["next_run_at"])
    op.create_table("collection_job",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False), sa.Column("active_key", sa.String(64), unique=True),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("collection_schedule.id")),
        sa.Column("retry_of", sa.Integer(), sa.ForeignKey("collection_job.id")),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("run.id")),
        sa.Column("stats", sa.JSON(), nullable=False), sa.Column("current_source_key", sa.String(256), nullable=False),
        sa.Column("error", sa.Text(), nullable=False), sa.Column("error_kind", sa.String(32), nullable=False),
        sa.Column("analysis_status", sa.String(24), nullable=False), sa.Column("dataset_version_ids", sa.JSON(), nullable=False),
        sa.Column("enriched_version_ids", sa.JSON(), nullable=False), sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    for field in ("status", "schedule_id", "run_id"):
        op.create_index(f"ix_collection_job_{field}", "collection_job", [field])
    op.create_table("collection_event",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("job_id", sa.Integer(), sa.ForeignKey("collection_job.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False), sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_collection_event_job_id", "collection_event", ["job_id"])


def downgrade():
    raise RuntimeError("Restore a backup to remove collection job history")
