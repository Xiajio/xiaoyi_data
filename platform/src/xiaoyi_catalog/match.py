"""混合检索（M2）：结构化硬过滤 + 向量语义排序。

排序理由随结果返回（命中的结构化条件 + 相似度分值），不做黑盒推荐。
无嵌入索引或 query 为空时退化为结构化检索（按下载量），并标注 ranking="downloads"。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .embeddings import TextEmbedder, cosine_top_k
from .search import SearchFilter, dataset_to_dict, search_datasets


def match_datasets(
    session: Session,
    filt: SearchFilter,
    *,
    query_text: str = "",
    embedder: TextEmbedder | None = None,
    vector_k: int = 50,
) -> dict:
    """结构化过滤为硬条件；query_text + 嵌入器存在时在过滤结果内按余弦重排。"""
    broad = search_datasets(session, SearchFilter(**{**filt.__dict__, "limit": 10_000, "offset": 0}))
    if not query_text.strip() or embedder is None:
        ranked = sorted(broad, key=lambda item: item["downloads"], reverse=True)[: filt.limit]
        return {"ranking": "downloads", "results": ranked}
    scores = dict(cosine_top_k(session, embedder.embed([query_text])[0], vector_k, producer=embedder.model_name))
    if not scores:
        ranked = sorted(broad, key=lambda item: item["downloads"], reverse=True)[: filt.limit]
        return {"ranking": "downloads", "note": "嵌入索引为空，未做语义排序", "results": ranked}
    for item in broad:
        item["similarity"] = round(scores.get(item["id"], 0.0), 4)
    ranked = sorted(broad, key=lambda item: (item["similarity"], item["downloads"]), reverse=True)[: filt.limit]
    return {"ranking": "structured+cosine", "results": ranked}


def requirement_to_filter(requirements: dict) -> SearchFilter:
    """把思路卡的 requirements 契约映射为结构化过滤；未知键忽略，保持向前兼容。"""
    return SearchFilter(
        modalities=tuple(requirements.get("modalities") or ()),
        task_categories=tuple(requirements.get("task_categories") or ()),
        languages=tuple(requirements.get("languages") or ()),
        license_classes=tuple(requirements.get("license_class") or ()),
        min_rows=requirements.get("min_rows"),
        limit=int(requirements.get("limit") or 10),
    )


def missing_for_requirement(item: dict, requirements: dict) -> list[str]:
    """相对思路卡需求的缺口提示：硬约束不满足时明确列出，供提案与人工核验参考。"""
    gaps: list[str] = []
    wanted_modalities = set(requirements.get("modalities") or ())
    if wanted_modalities and not wanted_modalities.intersection(item["modalities"]):
        gaps.append(f"模态不匹配：需要 {'/'.join(sorted(wanted_modalities))}")
    license_classes = set(requirements.get("license_class") or ())
    if license_classes and item["license_class"] not in license_classes:
        gaps.append(f"许可分级 {item['license_class']} 不在允许范围")
    min_rows = requirements.get("min_rows")
    if min_rows is not None:
        if item["size_rows"] is None:
            gaps.append("规模未知，无法确认满足最小行数")
        elif item["size_rows"] < min_rows:
            gaps.append(f"规模 {item['size_rows']} 行低于需求 {min_rows} 行")
    return gaps
