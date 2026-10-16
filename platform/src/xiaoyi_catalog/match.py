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
    broad = search_datasets(session, SearchFilter(**{**filt.__dict__, "limit": None, "offset": 0}))
    if not query_text.strip() or embedder is None:
        ranked = sorted(broad, key=lambda item: item["downloads"], reverse=True)[filt.offset : filt.offset + filt.limit]
        return {"ranking": "downloads", "results": ranked}
    scores = dict(cosine_top_k(session, embedder.embed([query_text])[0], len(broad), producer=embedder.model_name, dataset_ids=[item["id"] for item in broad]))
    if not scores:
        ranked = sorted(broad, key=lambda item: item["downloads"], reverse=True)[filt.offset : filt.offset + filt.limit]
        return {"ranking": "downloads", "note": "嵌入索引为空，未做语义排序", "results": ranked}
    for item in broad:
        item["similarity"] = round(scores.get(item["id"], 0.0), 4)
    ranked = sorted(broad, key=lambda item: (item["similarity"], item["downloads"]), reverse=True)[filt.offset : filt.offset + filt.limit]
    return {"ranking": "structured+cosine", "results": ranked}


def requirement_to_filter(requirements: dict) -> SearchFilter:
    """把思路卡的 requirements 契约映射为结构化过滤；未知键忽略，保持向前兼容。"""
    return SearchFilter(
        modalities=tuple(_values(requirements.get("modalities"))),
        task_categories=tuple(_values(requirements.get("task_categories"))),
        languages=tuple(_values(requirements.get("languages"))),
        license_classes=tuple(_values(requirements.get("license_classes") or requirements.get("license_class"))),
        min_rows=requirements.get("min_rows"),
        max_rows=requirements.get("max_rows"),
        min_bytes=requirements.get("min_bytes"),
        max_bytes=requirements.get("max_bytes"),
        limit=int(requirements.get("limit") or 10),
    )


def _values(value) -> list:
    return [value] if isinstance(value, str) else list(value or [])


def missing_for_requirement(item: dict, requirements: dict) -> list[str]:
    """The same OR-within-field / AND-between-fields constraints used by search, including unknowns."""
    gaps = []
    for field, label in (("modalities", "模态"), ("task_categories", "任务"), ("languages", "语言")):
        wanted = set(_values(requirements.get(field)))
        if wanted and not wanted.intersection(item.get(field) or []):
            gaps.append(f"{label}不匹配或未知：需要 {'/'.join(sorted(wanted))}")
    allowed = set(_values(requirements.get("license_classes") or requirements.get("license_class")))
    if allowed and (item.get("license_class") in (None, "unknown") or item.get("license_class") not in allowed):
        gaps.append(f"许可分级 {item.get('license_class', 'unknown')} 不在允许范围")
    for field, minimum in (("size_rows", requirements.get("min_rows")), ("size_bytes", requirements.get("min_bytes"))):
        if minimum is not None:
            actual = item.get(field)
            if actual is None:
                gaps.append("规模未知，无法确认满足最小行数" if field == "size_rows" else "字节规模未知")
            elif actual < minimum:
                gaps.append(f"规模 {actual} 低于需求 {minimum} ({field})")
    for field, maximum in (("size_rows", requirements.get("max_rows")), ("size_bytes", requirements.get("max_bytes"))):
        if maximum is not None:
            actual = item.get(field)
            if actual is None:
                gaps.append(f"规模未知，无法确认满足上限 ({field})")
            elif actual > maximum:
                gaps.append(f"规模 {actual} 超过上限 {maximum} ({field})")
    return gaps
