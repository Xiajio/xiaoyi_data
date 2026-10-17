"""Fenced resource jobs; legacy business IDs remain unchanged."""
from alembic import op
import sqlalchemy as sa
revision = "0009_jobs"
down_revision = "0008_trust"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("job",
        sa.Column("id",sa.Integer(),primary_key=True),
        sa.Column("kind",sa.String(64),nullable=False),sa.Column("queue",sa.String(24),nullable=False),
        sa.Column("actor",sa.String(128),nullable=False),sa.Column("request_key",sa.String(128),nullable=False),
        sa.Column("payload",sa.JSON(),nullable=False),sa.Column("payload_hash",sa.String(64),nullable=False),
        sa.Column("status",sa.String(24),nullable=False),sa.Column("owner",sa.String(128)),sa.Column("fence",sa.String(64)),
        sa.Column("attempts",sa.Integer(),nullable=False),sa.Column("max_attempts",sa.Integer(),nullable=False),
        sa.Column("lease_until",sa.DateTime(timezone=True)),sa.Column("available_at",sa.DateTime(timezone=True),nullable=False),
        sa.Column("cancel_requested",sa.Boolean(),nullable=False),sa.Column("result",sa.JSON(),nullable=False),
        sa.Column("error",sa.Text(),nullable=False),sa.Column("progress",sa.JSON(),nullable=False),
        sa.Column("finished_at",sa.DateTime(timezone=True)),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint("actor","request_key",name="uq_job_actor_request"))
    op.create_index("ix_job_claim","job",["queue","status","available_at"])
    op.create_table("job_attempt",
        sa.Column("id",sa.Integer(),primary_key=True),sa.Column("job_id",sa.Integer(),sa.ForeignKey("job.id"),nullable=False),
        sa.Column("number",sa.Integer(),nullable=False),sa.Column("owner",sa.String(128),nullable=False),sa.Column("fence",sa.String(64),nullable=False),
        sa.Column("status",sa.String(24),nullable=False),sa.Column("started_at",sa.DateTime(timezone=True),nullable=False),
        sa.Column("finished_at",sa.DateTime(timezone=True)),sa.Column("error",sa.Text(),nullable=False),
        sa.UniqueConstraint("job_id","number",name="uq_job_attempt"))
    op.create_index("ix_job_attempt_job_id","job_attempt",["job_id"])
    op.create_table("job_artifact",sa.Column("id",sa.Integer(),primary_key=True),
        sa.Column("job_id",sa.Integer(),sa.ForeignKey("job.id"),nullable=False,unique=True),
        sa.Column("content_hash",sa.String(64),nullable=False),sa.Column("manifest",sa.JSON(),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False))
    op.create_table("job_event",sa.Column("id",sa.Integer(),primary_key=True),
        sa.Column("job_id",sa.Integer(),sa.ForeignKey("job.id"),nullable=False),sa.Column("kind",sa.String(32),nullable=False),
        sa.Column("detail",sa.JSON(),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False))
    op.create_index("ix_job_event_job_id","job_event",["job_id"])

def downgrade():
    raise RuntimeError("Restore the complete pre-release backup; execution history must not be dropped")
