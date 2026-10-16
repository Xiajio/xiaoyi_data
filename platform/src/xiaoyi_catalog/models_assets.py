"""Accounts, approved acquisitions and immutable research bundle metadata."""
from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, Timestamped, utcnow
from datetime import datetime

class AuthUser(Timestamped, Base):
    __tablename__ = "auth_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True)
    password_hash: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(24), default="reader")
    kind: Mapped[str] = mapped_column(String(16), default="human")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

class AuthSession(Base):
    __tablename__ = "auth_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_user.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class DownloadRequest(Timestamped, Base):
    __tablename__ = "download_request"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_version.id"), index=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    approved_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    max_bytes: Mapped[int] = mapped_column(BigInteger)
    estimated_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    actual_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    decided_by: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

class Asset(Timestamped, Base):
    __tablename__ = "asset"
    __table_args__ = (UniqueConstraint("dataset_version_id", "path", name="uq_asset_version_path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_version.id"), index=True)
    download_request_id: Mapped[int] = mapped_column(ForeignKey("download_request.id"))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(24), default="downloaded")
    parent_asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    recipe: Mapped[dict] = mapped_column(JSON, default=dict)

class QualityReport(Timestamped, Base):
    __tablename__ = "quality_report"
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), unique=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_version.id"), index=True)
    producer: Mapped[str] = mapped_column(String(64), default="profile/1")
    value: Mapped[dict] = mapped_column(JSON, default=dict)

class ResearchBundle(Timestamped, Base):
    __tablename__ = "research_bundle"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshot.id"), unique=True)
    manifest: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(Text)
