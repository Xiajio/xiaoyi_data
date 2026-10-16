"""Adopt the existing unversioned catalogue without rewriting historical records."""
from alembic import op
import sqlalchemy as sa
revision = "0001_legacy_baseline"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    from xiaoyi_catalog.migrations.schema_v1 import create_missing
    connection = op.get_bind()
    # Existing installations keep their tables; new installations get the complete schema.
    if connection.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    create_missing(connection, {'audit', 'snapshot', 'dataset', 'run', 'idea', 'review', 'enrichment', 'source', 'pairing', 'dataset_version', 'evidence'})
    inspector = sa.inspect(connection)
    if "source_modified_at" not in {c["name"] for c in inspector.get_columns("dataset")}:
        op.add_column("dataset", sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True))

def downgrade():
    raise RuntimeError("Destructive catalogue downgrade is not supported; restore a database backup")
