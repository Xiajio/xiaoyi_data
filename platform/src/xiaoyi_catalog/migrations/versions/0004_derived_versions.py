"""Keep derived data lineage separate from the latest upstream version."""
from alembic import op
import sqlalchemy as sa
revision = "0004_derived_versions"
down_revision = "0003_vector_and_runtime"
branch_labels = None
depends_on = None

def upgrade():
    connection = op.get_bind()
    names = {c["name"] for c in sa.inspect(connection).get_columns("dataset_version")}
    if "kind" not in names:
        op.add_column("dataset_version", sa.Column("kind", sa.String(16), server_default="source", nullable=False))
        op.create_index("ix_dataset_version_kind", "dataset_version", ["kind"])
    if "parent_version_id" not in names:
        op.add_column("dataset_version", sa.Column("parent_version_id", sa.Integer(), nullable=True))
        if connection.dialect.name != "sqlite":
            op.create_foreign_key("fk_version_parent", "dataset_version", "dataset_version", ["parent_version_id"], ["id"])

def downgrade():
    raise RuntimeError("Restore a backup to remove dataset lineage")
