"""Immutable samples, measurements and experiment artifacts; preserve legacy reports."""
from alembic import op
import sqlalchemy as sa
revision = "0010_samples"
down_revision = "0009_jobs"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('dataset_build',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('requested_by', sa.String(length=128), primary_key=False, nullable=False),
        sa.Column('status', sa.String(length=24), primary_key=False, nullable=False),
        sa.Column('recipe', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('recipe_hash', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('input_manifest', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('manifest', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('manifest_hash', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('sample_count', sa.BigInteger(), primary_key=False, nullable=False),
        sa.Column('error', sa.Text(), primary_key=False, nullable=False),
        sa.Column('approved_by', sa.String(length=128), primary_key=False, nullable=False),
        sa.Column('approved_hash', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False))
    op.create_index('ix_dataset_build_recipe_hash', 'dataset_build', ['recipe_hash'], unique=False)
    op.create_index('ix_dataset_build_status', 'dataset_build', ['status'], unique=False)
    op.create_table('entity_group',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('namespace', sa.String(length=64), primary_key=False, nullable=False))
    op.create_table('annotation_set',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('build_id', sa.Integer(), sa.ForeignKey('dataset_build.id'), primary_key=False, nullable=False),
        sa.Column('version_hash', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('extraction', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.UniqueConstraint('build_id', name=None))
    op.create_table('experiment',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('build_id', sa.Integer(), sa.ForeignKey('dataset_build.id'), primary_key=False, nullable=False),
        sa.Column('requested_by', sa.String(length=128), primary_key=False, nullable=False),
        sa.Column('status', sa.String(length=24), primary_key=False, nullable=False),
        sa.Column('model', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('protocol', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('prediction_asset_id', sa.Integer(), sa.ForeignKey('asset.id'), primary_key=False, nullable=False),
        sa.Column('prediction_sha256', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('manifest', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('metrics', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('error', sa.Text(), primary_key=False, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False))
    op.create_index('ix_experiment_build_id', 'experiment', ['build_id'], unique=False)
    op.create_table('sample',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('dataset_version_id', sa.Integer(), sa.ForeignKey('dataset_version.id'), primary_key=False, nullable=False),
        sa.Column('asset_id', sa.Integer(), sa.ForeignKey('asset.id'), primary_key=False, nullable=False),
        sa.Column('source_position', sa.String(length=256), primary_key=False, nullable=False),
        sa.Column('content_hash', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('modality', sa.String(length=24), primary_key=False, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False))
    op.create_index('ix_sample_dataset_version_id', 'sample', ['dataset_version_id'], unique=False)
    op.create_index('ix_sample_content_hash', 'sample', ['content_hash'], unique=False)
    op.create_index('ix_sample_asset_id', 'sample', ['asset_id'], unique=False)
    op.create_table('asset_reference',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('sample_id', sa.String(length=64), sa.ForeignKey('sample.id'), primary_key=False, nullable=False),
        sa.Column('asset_id', sa.Integer(), sa.ForeignKey('asset.id'), primary_key=False, nullable=False),
        sa.Column('role', sa.String(length=24), primary_key=False, nullable=False),
        sa.UniqueConstraint('sample_id', 'asset_id', name='uq_sample_asset_reference'))
    op.create_index('ix_asset_reference_sample_id', 'asset_reference', ['sample_id'], unique=False)
    op.create_table('build_sample',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('build_id', sa.Integer(), sa.ForeignKey('dataset_build.id'), primary_key=False, nullable=False),
        sa.Column('sample_id', sa.String(length=64), sa.ForeignKey('sample.id'), primary_key=False, nullable=False),
        sa.Column('split', sa.String(length=24), primary_key=False, nullable=False),
        sa.Column('group_key', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('shard', sa.Integer(), primary_key=False, nullable=False),
        sa.Column('row_index', sa.Integer(), primary_key=False, nullable=False),
        sa.UniqueConstraint('build_id', 'sample_id', name='uq_build_sample'))
    op.create_index('ix_build_sample_build_id', 'build_sample', ['build_id'], unique=False)
    op.create_index('ix_build_sample_sample_id', 'build_sample', ['sample_id'], unique=False)
    op.create_index('ix_build_sample_split', 'build_sample', ['split'], unique=False)
    op.create_table('profile_run',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('build_id', sa.Integer(), sa.ForeignKey('dataset_build.id'), primary_key=False, nullable=True),
        sa.Column('legacy_report_id', sa.Integer(), sa.ForeignKey('quality_report.id'), primary_key=False, nullable=True),
        sa.Column('producer', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('status', sa.String(length=24), primary_key=False, nullable=False),
        sa.Column('manifest', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('summary', sa.JSON(), primary_key=False, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.UniqueConstraint('legacy_report_id', name=None))
    op.create_index('ix_profile_run_build_id', 'profile_run', ['build_id'], unique=False)
    op.create_table('metric_observation',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('profile_run_id', sa.Integer(), sa.ForeignKey('profile_run.id'), primary_key=False, nullable=False),
        sa.Column('name', sa.String(length=128), primary_key=False, nullable=False),
        sa.Column('value', sa.JSON(), primary_key=False, nullable=False))
    op.create_index('ix_metric_observation_profile_run_id', 'metric_observation', ['profile_run_id'], unique=False)
    op.create_table('sample_relation',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('profile_run_id', sa.Integer(), sa.ForeignKey('profile_run.id'), primary_key=False, nullable=False),
        sa.Column('source_sample_id', sa.String(length=64), sa.ForeignKey('sample.id'), primary_key=False, nullable=False),
        sa.Column('target_sample_id', sa.String(length=64), sa.ForeignKey('sample.id'), primary_key=False, nullable=False),
        sa.Column('kind', sa.String(length=32), primary_key=False, nullable=False),
        sa.Column('algorithm', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('score', sa.Double(), primary_key=False, nullable=False))
    op.create_index('ix_sample_relation_target_sample_id', 'sample_relation', ['target_sample_id'], unique=False)
    op.create_index('ix_sample_relation_profile_run_id', 'sample_relation', ['profile_run_id'], unique=False)
    op.create_index('ix_sample_relation_source_sample_id', 'sample_relation', ['source_sample_id'], unique=False)
    op.create_index("ix_build_sample_page", "build_sample", ["build_id", "id"])
    op.create_index("ix_sample_relation_page", "sample_relation", ["profile_run_id", "id"])
    # Copy historical measured reports verbatim; no source declarations become measurements.
    connection = op.get_bind()
    old = sa.table("quality_report", sa.column("id",sa.Integer()), sa.column("asset_id",sa.Integer()),
                   sa.column("producer",sa.String()),sa.column("value",sa.JSON()),
                   sa.column("created_at",sa.DateTime()),sa.column("updated_at",sa.DateTime()))
    profiles = sa.table("profile_run", sa.column("legacy_report_id",sa.Integer()),sa.column("producer",sa.String()),
        sa.column("status",sa.String()),sa.column("manifest",sa.JSON()),sa.column("summary",sa.JSON()),
        sa.column("created_at",sa.DateTime()),sa.column("updated_at",sa.DateTime()))
    result = connection.execute(sa.select(old)).mappings()
    while batch := result.fetchmany(500):
        connection.execute(profiles.insert(), [dict(legacy_report_id=row["id"],producer=row["producer"],status="completed",
            manifest={"legacy_asset_id":row["asset_id"],"migration":"0010_samples"},summary=row["value"],
            created_at=row["created_at"],updated_at=row["updated_at"]) for row in batch])


def downgrade():
    raise RuntimeError("Restore a verified backup to remove sample and experiment history")
