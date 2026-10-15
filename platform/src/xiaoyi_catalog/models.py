"""目录库表结构（M1 范围：source / dataset / dataset_version / evidence / run）。

设计约定见 docs/dataset-platform-design.md 第 4 节：
- 多值属性（模态、任务、语言）存 JSON 数组；缺失即为空数组，检索侧以 unknown 表达。
- fingerprint 是跨采集的去重键：sha256(kind:source_key:revision)。
- run 记录每次采集的输入范围与统计，失败分类写入 error_kind。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Source(Timestamped, Base):
    """数据源适配器注册；cursor 存增量水位（如 {"last_modified": ...}）。"""

    __tablename__ = "source"
    __table_args__ = (UniqueConstraint("kind", "endpoint", name="uq_source_kind_endpoint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_limit: Mapped[float | None] = mapped_column(default=None)
    cursor: Mapped[dict | None] = mapped_column(JSON, default=None)


class Dataset(Timestamped, Base):
    """统一目录记录。license_class ∈ permissive/copyleft/research-only/unknown。"""

    __tablename__ = "dataset"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(256), index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    author: Mapped[str] = mapped_column(String(256), default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    license: Mapped[str] = mapped_column(String(128), default="")
    license_class: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    modalities: Mapped[list] = mapped_column(JSON, default=list)
    modality_source: Mapped[str] = mapped_column(String(16), default="unknown")
    task_categories: Mapped[list] = mapped_column(JSON, default=list)
    languages: Mapped[list] = mapped_column(JSON, default=list)
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    size_rows: Mapped[int | None] = mapped_column(Integer, default=None)
    downloads: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    card_text: Mapped[str] = mapped_column(Text, default="")
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DatasetVersion(Timestamped, Base):
    """数据集版本钉住：指纹随 revision 变化，配对与快照后续引用具体版本。"""

    __tablename__ = "dataset_version"
    __table_args__ = (UniqueConstraint("dataset_id", "revision", name="uq_dataset_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id"), index=True)
    revision: Mapped[str] = mapped_column(String(128), default="")
    changelog: Mapped[str] = mapped_column(Text, default="")


class Evidence(Base):
    """证据片段：卡片摘录 / README / 样本 / schema。自动结论必须能指到证据。"""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    excerpt: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(256), default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    """采集任务执行记录。status ∈ running/completed/failed/interrupted。"""

    __tablename__ = "run"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="collect")
    adapter: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    input_scope: Mapped[dict] = mapped_column(JSON, default=dict)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error_kind: Mapped[str] = mapped_column(String(16), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    retry_of: Mapped[int | None] = mapped_column(Integer, default=None)
