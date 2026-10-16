"""Version provenance and evidence bindings; legacy metadata is intentionally not fabricated."""
from alembic import op
import sqlalchemy as sa
revision = "0002_immutable_versions"
down_revision = "0001_legacy_baseline"
branch_labels = None
depends_on = None

def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    columns = {c["name"] for c in inspector.get_columns("dataset_version")}
    if "metadata_json" not in columns:
        op.add_column("dataset_version", sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"))
    if "provenance_status" not in columns:
        op.add_column("dataset_version", sa.Column("provenance_status", sa.String(32), nullable=False, server_default="legacy_unverified"))
        op.create_index("ix_dataset_version_provenance_status", "dataset_version", ["provenance_status"])
    for table in ("evidence", "enrichment"):
        if "dataset_version_id" not in {c["name"] for c in sa.inspect(connection).get_columns(table)}:
            # Nullable preserves legacy rows without falsely associating them with a revision.
            op.add_column(table, sa.Column("dataset_version_id", sa.Integer(), nullable=True))
            op.create_index(f"ix_{table}_dataset_version_id", table, ["dataset_version_id"])
            if connection.dialect.name != "sqlite":
                op.create_foreign_key(f"fk_{table}_version", table, "dataset_version", ["dataset_version_id"], ["id"])
    if connection.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

def downgrade():
    raise RuntimeError("Version provenance must not be dropped; restore a database backup")
