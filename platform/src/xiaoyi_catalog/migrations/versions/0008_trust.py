"""Bind new reviews to immutable revisions without inventing historical proof."""
from alembic import op
import sqlalchemy as sa

revision = "0008_trust"
down_revision = "0007_operations"
branch_labels = None
depends_on = None


def upgrade():
    # Defaults explicitly identify pre-migration rows; no old values or reviews are overwritten.
    op.add_column("enrichment", sa.Column("content_hash", sa.String(64), nullable=False, server_default=""))
    op.add_column("enrichment", sa.Column("history_status", sa.String(32), nullable=False, server_default="legacy_unverified"))
    op.add_column("enrichment", sa.Column("parent_enrichment_id", sa.Integer(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key("fk_enrichment_parent", "enrichment", "enrichment", ["parent_enrichment_id"], ["id"])
    op.add_column("review", sa.Column("target_content_hash", sa.String(64), nullable=False, server_default=""))
    op.add_column("review", sa.Column("target_snapshot", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("review", sa.Column("history_status", sa.String(32), nullable=False, server_default="legacy_unverified"))
    op.add_column("review", sa.Column("resulting_enrichment_id", sa.Integer(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key("fk_review_result", "review", "enrichment", ["resulting_enrichment_id"], ["id"])


def downgrade():
    raise RuntimeError("Trust history is append-only; restore a tested backup rather than deleting review proof")
