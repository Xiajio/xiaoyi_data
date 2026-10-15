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
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
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


class Enrichment(Timestamped, Base):
    """自动整理结果，一律候选状态（status ∈ candidate/confirmed/rejected/superseded）。

    kind ∈ schema/quality/embedding；producer 记录产出版本（如 enrich/0.1、嵌入模型名）。
    自动产出必须挂证据（evidence_ids），嵌入例外：其证据是被嵌入的卡片证据本身。
    """

    __tablename__ = "enrichment"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    producer: Mapped[str] = mapped_column(String(128), default="")
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[str] = mapped_column(String(16), default="")
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="candidate", index=True)


class Idea(Base):
    """来自文献挖掘智能体（小亿灵析）的思路卡，按 external_id 幂等导入。"""

    __tablename__ = "idea"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    statement: Mapped[str] = mapped_column(Text, default="")
    requirements: Mapped[dict] = mapped_column(JSON, default=dict)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Pairing(Timestamped, Base):
    """思路 × 数据集版本提案。智能体只能创建 pending；确认/驳回由人工完成。"""

    __tablename__ = "pairing"
    __table_args__ = (UniqueConstraint("idea_id", "dataset_version_id", name="uq_pairing_idea_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    idea_id: Mapped[int] = mapped_column(ForeignKey("idea.id"), index=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_version.id"), index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    proposed_by: Mapped[str] = mapped_column(String(32), default="agent")
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)


class Snapshot(Timestamped, Base):
    """思路 × 数据集组合的冻结版本；同一思路同时最多一个 pending。"""

    __tablename__ = "snapshot"
    __table_args__ = (UniqueConstraint("idea_id", "version", name="uq_snapshot_idea_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    idea_id: Mapped[int] = mapped_column(ForeignKey("idea.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    pairing_ids: Mapped[list] = mapped_column(JSON, default=list)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Review(Base):
    """人工核验留痕：target_kind ∈ enrichment/pairing，decision ∈ confirmed/rejected。"""

    __tablename__ = "review"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(16), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(32), default="reviewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Audit(Base):
    """服务侧操作审计（内部测试为本地记录，不具备不可篡改能力）。"""

    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), default="api")
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
