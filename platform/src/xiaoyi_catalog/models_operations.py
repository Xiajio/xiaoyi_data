"""Operational records: process health and migration provenance, never approval authority."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, utcnow


class WorkerHeartbeatRecord(Base):
    __tablename__ = "worker_heartbeat"
    owner: Mapped[str] = mapped_column(String(64), primary_key=True)
    queue: Mapped[str] = mapped_column(String(24), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MigrationRecord(Base):
    __tablename__ = "migration_record"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_revision: Mapped[str] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    checkpoint_archive: Mapped[str] = mapped_column(Text, default="")
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MigratedRun(Base):
    __tablename__ = "migrated_run"
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_run.id"), primary_key=True)
    migration_id: Mapped[str] = mapped_column(ForeignKey("migration_record.id"))
    checkpoint_policy: Mapped[str] = mapped_column(String(32), default="archived")
