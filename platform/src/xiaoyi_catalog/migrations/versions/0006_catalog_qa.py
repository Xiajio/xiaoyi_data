"""Private persistent catalog Q&A; no source or approval mutation authority."""
from alembic import op
import sqlalchemy as sa
revision = "0006_catalog_qa"
down_revision = "0005_collection_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("qa_conversation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_qa_conversation_actor", "qa_conversation", ["actor"])
    op.create_table("qa_turn",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("qa_conversation.id"), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("answer", sa.JSON()),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(256), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "request_key", name="uq_qa_turn_request"))
    op.create_index("ix_qa_turn_conversation_id", "qa_turn", ["conversation_id"])
    op.create_index("ix_qa_turn_status", "qa_turn", ["status"])


def downgrade():
    raise RuntimeError("Restore a backup to remove private Q&A history")
