"""Durable source collection jobs and interval schedules, independent of LLM runs."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, Timestamped, utcnow


class CollectionSchedule(Timestamped, Base):
    __tablename__ = "collection_schedule"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    scope: Mapped[dict] = mapped_column(JSON)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128))


class CollectionJob(Timestamped, Base):
    __tablename__ = "collection_job"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(128))
    scope: Mapped[dict] = mapped_column(JSON)
    active_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(24), default="queued")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("collection_schedule.id"), nullable=True, index=True)
    retry_of: Mapped[int | None] = mapped_column(ForeignKey("collection_job.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("run.id"), nullable=True, index=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    current_source_key: Mapped[str] = mapped_column(String(256), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    error_kind: Mapped[str] = mapped_column(String(32), default="")
    analysis_status: Mapped[str] = mapped_column(String(24), default="pending")
    dataset_version_ids: Mapped[list] = mapped_column(JSON, default=list)
    enriched_version_ids: Mapped[list] = mapped_column(JSON, default=list)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CollectionEvent(Base):
    __tablename__ = "collection_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("collection_job.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Schedule = CollectionSchedule
