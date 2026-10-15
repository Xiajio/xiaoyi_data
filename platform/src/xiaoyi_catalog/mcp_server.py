"""MCP server：把目录能力暴露为智能体工具（stdio）。

五个工具（对应设计文档第 6 节）：
  dataset_search    结构化需求 + 可选语义检索，返回候选与证据指针
  dataset_evidence  数据集证据（卡片摘录、schema 摘要、质量信号、缺失项）
  pairing_propose   思路卡 × 数据集版本提案，只能落 pending
  snapshot_status   查询思路下配对与快照状态
  snapshot_export   导出已批准快照（未批准报错）

运行：python -m xiaoyi_catalog.mcp_server
配置：XIAOYI_CATALOG_DB 指定库路径；嵌入模型默认懒加载 bge-small-zh-v1.5，
      XIAOYI_CATALOG_EMBED=0 关闭语义排序（退化为结构化检索）。
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .db import create_engine_for, session_scope
from .embeddings import SentenceTransformerEmbedder, TextEmbedder
from .match import match_datasets, requirement_to_filter
from .models import Dataset, Evidence, Enrichment
from .pairing import export_snapshot as _export_snapshot
from .pairing import propose_pairing, snapshot_status as _snapshot_status
from .search import SearchFilter, dataset_to_dict

from sqlalchemy import select

mcp = FastMCP("xiaoyi-catalog")

_embedder: TextEmbedder | None = None
_embedder_failed = False


def _get_embedder() -> TextEmbedder | None:
    global _embedder, _embedder_failed
    if os.environ.get("XIAOYI_CATALOG_EMBED", "1") == "0" or _embedder_failed:
        return None
    if _embedder is None:
        try:
            _embedder = SentenceTransformerEmbedder(os.environ.get("XIAOYI_CATALOG_EMBED_MODEL", "BAAI/bge-small-zh-v1.5"))
            _embedder.embed(["预热"])
        except Exception:
            _embedder_failed = True
            return None
    return _embedder


def _engine():
    return create_engine_for(os.environ.get("XIAOYI_CATALOG_DB", "./data/catalog.db"))


@mcp.tool()
def dataset_search(requirements: dict, query: str = "") -> dict:
    """按思路卡需求检索数据集。requirements 键：modalities/task_categories/languages/
    license_class/min_rows/limit；query 为自由文本（有嵌入索引时参与语义排序）。
    返回候选列表（含 missing 缺口与 similarity）与 ranking 说明。"""
    filt = requirement_to_filter(requirements)
    engine = _engine()
    try:
        with session_scope(engine) as session:
            return match_datasets(session, filt, query_text=query, embedder=_get_embedder())
    finally:
        engine.dispose()


@mcp.tool()
def dataset_evidence(dataset_id: int) -> dict:
    """返回数据集的目录信息、卡片证据摘录、schema 摘要与质量信号候选。"""
    engine = _engine()
    try:
        with session_scope(engine) as session:
            dataset = session.get(Dataset, dataset_id)
            if dataset is None:
                return {"error": f"数据集 {dataset_id} 不存在"}
            evidence = session.scalars(select(Evidence).where(Evidence.dataset_id == dataset_id)).all()
            enrichments = session.scalars(
                select(Enrichment).where(Enrichment.dataset_id == dataset_id, Enrichment.kind.in_(["schema", "quality"]))
            ).all()
            return {
                "dataset": dataset_to_dict(dataset),
                "evidence": [{"id": e.id, "kind": e.kind, "location": e.location, "excerpt": e.excerpt[:800]} for e in evidence],
                "enrichment": [{"id": en.id, "kind": en.kind, "producer": en.producer, "status": en.status,
                                "value": en.value, "evidence_ids": en.evidence_ids} for en in enrichments],
            }
    finally:
        engine.dispose()


@mcp.tool()
def pairing_propose(idea: dict, dataset_version_ids: list[int], rationale: str) -> dict:
    """提交思路 × 数据集版本配对提案。提案一律 pending，需人工核验；智能体不能自我确认。"""
    engine = _engine()
    try:
        with session_scope(engine) as session:
            return propose_pairing(session, idea, dataset_version_ids, rationale)
    finally:
        engine.dispose()


@mcp.tool()
def snapshot_status(idea_external_id: str) -> dict:
    """查询思路卡下的配对与快照状态。"""
    engine = _engine()
    try:
        with session_scope(engine) as session:
            return _snapshot_status(session, idea_external_id)
    finally:
        engine.dispose()


@mcp.tool()
def snapshot_export(snapshot_id: int) -> dict:
    """导出已批准快照的冻结内容；未批准时报错。含许可清单与声明。"""
    engine = _engine()
    try:
        with session_scope(engine) as session:
            return _export_snapshot(session, snapshot_id)
    finally:
        engine.dispose()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
