"""混合检索（M2）：结构化硬过滤 + 向量语义排序。

排序理由随结果返回（命中的结构化条件 + 相似度分值），不做黑盒推荐。
无嵌入索引或 query 为空时退化为结构化检索（按下载量），并标注 ranking="downloads"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from sqlalchemy.orm import Session
from .task_spec import evaluate_task_spec, validate_task_spec, requirement_gaps

from .embeddings import TextEmbedder, cosine_top_k
from .search import SearchFilter, dataset_to_dict, search_datasets, version_to_dict
from .models import DatasetVersion


@dataclass(frozen=True)
class TaskFilter(SearchFilter):
    task_spec: dict = field(default_factory=dict)


def match_datasets(
    session: Session,
    filt: SearchFilter,
    *,
    query_text: str = "",
    embedder: TextEmbedder | None = None,
    vector_k: int = 50,
) -> dict:
    """Compatibility facade; all consumers use the bounded retrieval service."""
    from .retrieval import RetrievalService
    return RetrievalService(session, embedder=embedder).search(filt, query_text=query_text)


def requirement_to_filter(requirements: dict) -> SearchFilter:
    """Carry the complete contract through every legacy matching entry point."""
    spec = validate_task_spec(requirements)
    # Nested alternatives must be evaluated before pagination, never flattened into SQL AND.
    return TaskFilter(limit=int(spec.get("limit") or 10), task_spec=spec)


def missing_for_requirement(item: dict, requirements: dict) -> list[str]:
    return requirement_gaps(item, requirements)
