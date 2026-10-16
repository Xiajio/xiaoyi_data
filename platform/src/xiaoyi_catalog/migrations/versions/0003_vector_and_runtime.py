"""Add the vector mirror and durable execution/asset/account tables."""
from alembic import op
revision = "0003_vector_and_runtime"
down_revision = "0002_immutable_versions"
branch_labels = None
depends_on = None

def upgrade():
    from xiaoyi_catalog.migrations.schema_v1 import create_missing
    connection = op.get_bind()
    create_missing(connection, {'agent_event', 'asset', 'auth_session', 'auth_user', 'research_bundle', 'vector_embedding', 'download_request', 'worker_lease', 'agent_tool_call', 'agent_run', 'quality_report'})
    if connection.dialect.name == "postgresql":
        import sqlalchemy as sa
        op.alter_column("dataset", "size_bytes", type_=sa.BigInteger())
        op.alter_column("evidence", "location", type_=sa.Text())

def downgrade():
    raise RuntimeError("Restore a backup to roll back durable research state")
