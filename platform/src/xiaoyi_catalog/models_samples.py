"""Immutable sample builds, annotations, quality observations and experiment provenance."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, Timestamped, utcnow

class Sample(Base):
    __tablename__ = "sample"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_version.id"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    source_position: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    modality: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class AssetReference(Base):
    __tablename__ = "asset_reference"
    __table_args__ = (UniqueConstraint("sample_id", "asset_id", name="uq_sample_asset_reference"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_id: Mapped[str] = mapped_column(ForeignKey("sample.id"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"))
    role: Mapped[str] = mapped_column(String(24), default="source")

class DatasetBuild(Timestamped, Base):
    __tablename__ = "dataset_build"
    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    recipe: Mapped[dict] = mapped_column(JSON)
    recipe_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_manifest: Mapped[list] = mapped_column(JSON)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest_hash: Mapped[str] = mapped_column(String(64), default="")
    sample_count: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    approved_by: Mapped[str] = mapped_column(String(128), default="")
    approved_hash: Mapped[str] = mapped_column(String(64), default="")

class BuildSample(Base):
    __tablename__ = "build_sample"
    __table_args__ = (UniqueConstraint("build_id", "sample_id", name="uq_build_sample"), Index("ix_build_sample_page", "build_id", "id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("dataset_build.id"), index=True)
    sample_id: Mapped[str] = mapped_column(ForeignKey("sample.id"), index=True)
    split: Mapped[str] = mapped_column(String(24), index=True)
    group_key: Mapped[str] = mapped_column(String(64))
    shard: Mapped[int] = mapped_column()
    row_index: Mapped[int] = mapped_column()

class AnnotationSet(Base):
    __tablename__ = "annotation_set"
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("dataset_build.id"), unique=True)
    version_hash: Mapped[str] = mapped_column(String(64))
    extraction: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class EntityGroup(Base):
    __tablename__ = "entity_group"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64))

class SampleRelation(Base):
    __tablename__ = "sample_relation"
    __table_args__ = (Index("ix_sample_relation_page", "profile_run_id", "id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_run_id: Mapped[int] = mapped_column(ForeignKey("profile_run.id"), index=True)
    source_sample_id: Mapped[str] = mapped_column(ForeignKey("sample.id"), index=True)
    target_sample_id: Mapped[str] = mapped_column(ForeignKey("sample.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    algorithm: Mapped[str] = mapped_column(String(64))
    score: Mapped[float] = mapped_column()

class ProfileRun(Timestamped, Base):
    __tablename__ = "profile_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int | None] = mapped_column(ForeignKey("dataset_build.id"), index=True, default=None)
    legacy_report_id: Mapped[int | None] = mapped_column(ForeignKey("quality_report.id"), unique=True, default=None)
    producer: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="completed")
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)

class MetricObservation(Base):
    __tablename__ = "metric_observation"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_run_id: Mapped[int] = mapped_column(ForeignKey("profile_run.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    value: Mapped[dict] = mapped_column(JSON)

class Experiment(Timestamped, Base):
    __tablename__ = "experiment"
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("dataset_build.id"), index=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    model: Mapped[dict] = mapped_column(JSON)
    protocol: Mapped[dict] = mapped_column(JSON)
    prediction_asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"))
    prediction_sha256: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
