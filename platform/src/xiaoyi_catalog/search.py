"""目录结构化检索（M1：纯 SQL/内存过滤；向量语义检索属 M2）。

匹配语义：同一条件字段内多个取值是 OR（任一命中即匹配），不同字段之间是 AND。
结果附带 missing 清单，明确标出未知属性——不允许把缺失当作不存在。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import cast, func, or_, select, String
from sqlalchemy.orm import Session

from .models import Dataset, DatasetVersion, Evidence, Run
from .normalize import missing_fields


@dataclass(frozen=True)
class SearchFilter:
    modalities: tuple[str, ...] = ()
    task_categories: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    license_classes: tuple[str, ...] = ()
    query: str = ""
    min_rows: int | None = None
    max_rows: int | None = None
    min_bytes: int | None = None
    max_bytes: int | None = None
    limit: int | None = 20
    offset: int = 0


def _json_contains_any(column, values: tuple[str, ...]):
    return or_(*[cast(column, String).like(f'%"{value}"%') for value in values])


def dataset_to_dict(row: Dataset) -> dict:
    return {
        "id": row.id,
        "source_key": row.source_key,
        "name": row.name,
        "author": row.author,
        "url": row.url,
        "license": row.license,
        "license_class": row.license_class,
        "modalities": row.modalities,
        "modality_source": row.modality_source,
        "task_categories": row.task_categories,
        "languages": row.languages,
        "size_rows": row.size_rows,
        "size_bytes": row.size_bytes,
        "downloads": row.downloads,
        "likes": row.likes,
        "missing": missing_fields(row.modalities, row.license_class, row.card_text),
    }


def latest_version(session: Session, dataset_id: int) -> DatasetVersion | None:
    return session.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id, DatasetVersion.kind == "source")
                          .order_by(DatasetVersion.id.desc()))


def version_to_dict(session: Session, version: DatasetVersion) -> dict:
    """Read only the captured version; never infer historical metadata from the live row."""
    import copy
    metadata = copy.deepcopy(version.metadata_json or {})
    dataset = session.get(Dataset, version.dataset_id)
    metadata.setdefault("id", version.dataset_id)
    metadata.setdefault("source_key", dataset.source_key if dataset else "")
    for key, value in {"name": "", "author": "", "url": "", "license": "", "license_class": "unknown",
                       "modalities": [], "modality_source": "unknown", "task_categories": [], "languages": [],
                       "size_rows": None, "size_bytes": None, "downloads": 0, "likes": 0, "card_text": ""}.items():
        metadata.setdefault(key, value)
    metadata["dataset_version_id"] = version.id
    metadata["revision"] = version.revision
    metadata["version_kind"] = version.kind
    metadata["parent_version_id"] = version.parent_version_id
    metadata["provenance_status"] = version.provenance_status
    metadata["missing"] = missing_fields(metadata["modalities"], metadata["license_class"], metadata["card_text"])
    return metadata


def search_datasets(session: Session, filt: SearchFilter) -> list[dict]:
    stmt = select(Dataset).where(Dataset.status == "active")
    if filt.modalities:
        stmt = stmt.where(_json_contains_any(Dataset.modalities, filt.modalities))
    if filt.task_categories:
        stmt = stmt.where(_json_contains_any(Dataset.task_categories, filt.task_categories))
    if filt.languages:
        stmt = stmt.where(_json_contains_any(Dataset.languages, filt.languages))
    if filt.license_classes:
        stmt = stmt.where(Dataset.license_class.in_(filt.license_classes))
    if filt.min_rows is not None:
        stmt = stmt.where(Dataset.size_rows.is_not(None), Dataset.size_rows >= filt.min_rows)
    for column, minimum, maximum in ((Dataset.size_rows, None, filt.max_rows),
                                      (Dataset.size_bytes, filt.min_bytes, filt.max_bytes)):
        if minimum is not None:
            stmt = stmt.where(column.is_not(None), column >= minimum)
        if maximum is not None:
            stmt = stmt.where(column.is_not(None), column <= maximum)
    if filt.query:
        pattern = f"%{filt.query}%"
        stmt = stmt.where(or_(Dataset.name.like(pattern), Dataset.source_key.like(pattern), Dataset.card_text.like(pattern)))
    spec = getattr(filt, "task_spec", None)
    stmt = stmt.order_by(Dataset.downloads.desc(), Dataset.id)
    if spec is None:
        stmt = stmt.limit(filt.limit).offset(filt.offset)
    results = []
    for row in session.scalars(stmt):
        item = dataset_to_dict(row)
        version = latest_version(session, row.id)
        item.update(dataset_version_id=version.id if version else None,
                    revision=version.revision if version else None,
                    provenance_status=version.provenance_status if version else "legacy_unverified")
        if spec is not None:
            from .task_spec import evaluate_task_spec
            evaluation = evaluate_task_spec(version_to_dict(session, version) if version else {}, spec)
            if evaluation["status"] != "pass":
                continue
            item["constraint_evaluation"] = evaluation
        results.append(item)
    if spec is not None:
        return results[filt.offset:] if filt.limit is None else results[filt.offset:filt.offset + filt.limit]
    return results


def catalog_stats(session: Session) -> dict:
    """目录概览：总量、按来源 / 许可分级 / 模态分布、采集次数。"""
    total = session.scalar(select(func.count(Dataset.id)).where(Dataset.status == "active")) or 0
    by_license = dict(session.execute(
        select(Dataset.license_class, func.count()).where(Dataset.status == "active").group_by(Dataset.license_class)
    ).all())
    by_modality: dict[str, int] = {}
    for (modalities,) in session.execute(select(Dataset.modalities).where(Dataset.status == "active")).all():
        for modality in modalities or ["unknown"]:
            by_modality[modality] = by_modality.get(modality, 0) + 1
    by_source = dict(session.execute(
        select(Dataset.source_id, func.count()).where(Dataset.status == "active").group_by(Dataset.source_id)
    ).all())
    runs = session.scalar(select(func.count(Run.id))) or 0
    evidence = session.scalar(select(func.count(Evidence.id))) or 0
    return {
        "datasets": total,
        "by_license_class": by_license,
        "by_modality": dict(sorted(by_modality.items())),
        "by_source_id": by_source,
        "runs": runs,
        "evidence": evidence,
    }
