"""整理流水线（M2）：schema 摘要 + 质量信号，产出 enrichment 候选记录。

schema 摘要不访问 datasets-server：M1 存下的卡片原文含 YAML frontmatter，
其中的 dataset_info.features / splits 足以离线提取结构信息。所有产出
status=candidate、挂证据指针；人工确认在 M3 策展 UI 进行。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import yaml
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from .db import session_scope
from .models import Dataset, DatasetVersion, Enrichment, Evidence
from .search import latest_version, version_to_dict

PRODUCER = "enrich/0.2"
FRONTMATTER_LIMIT = 64_000


def parse_card_frontmatter(card_text: str) -> dict:
    """解析 dataset card 的 YAML frontmatter；无 frontmatter 或解析失败返回空 dict。"""
    if not card_text.startswith("---"):
        return {}
    end = card_text.find("\n---", 3)
    if end == -1 or end > FRONTMATTER_LIMIT:
        return {}
    try:
        parsed = yaml.safe_load(card_text[3:end])
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def schema_summary(card_text: str) -> dict:
    """从卡片 frontmatter 提取结构摘要：特征列、配置与切片规模。缺什么标什么。"""
    meta = parse_card_frontmatter(card_text)
    dataset_info = meta.get("dataset_info")
    if not isinstance(dataset_info, dict):
        return {"present": False}
    summary: dict = {"present": True}
    features = dataset_info.get("features")
    if isinstance(features, list):
        summary["features"] = [
            {"name": str(item.get("name", "")), "dtype": str(item.get("dtype", item.get("type", "")))}
            for item in features if isinstance(item, dict)
        ][:50]
    splits = dataset_info.get("splits")
    if isinstance(splits, list):
        summary["splits"] = [
            {"name": str(item.get("name", "")), "num_examples": item.get("num_examples")}
            for item in splits if isinstance(item, dict)
        ]
        total = sum(int(item["num_examples"]) for item in summary["splits"] if isinstance(item.get("num_examples"), (int, float)))
        if total:
            summary["total_examples"] = total
    if isinstance(dataset_info.get("dataset_size"), (int, float)):
        summary["dataset_size"] = int(dataset_info["dataset_size"])
    if isinstance(dataset_info.get("download_size"), (int, float)):
        summary["download_size"] = int(dataset_info["download_size"])
    return summary


def quality_signals(dataset: Dataset, *, now: datetime | None = None) -> dict:
    """质量分级（low/medium/high）+ 逐条依据。分级是启发式规则，不是统计置信度。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    reasons: list[str] = []
    score = 0
    card_len = len(dataset.card_text.strip())
    if card_len >= 500:
        score += 2
        reasons.append("卡片内容充实（≥500 字符）")
    elif card_len > 0:
        score += 1
        reasons.append("卡片内容较少")
    else:
        reasons.append("缺少数据集卡片")
    if dataset.license_class != "unknown":
        score += 1
        reasons.append(f"许可已标注（{dataset.license_class}）")
    else:
        reasons.append("许可未知")
    if dataset.downloads >= 1000:
        score += 1
        reasons.append(f"下载量 {dataset.downloads} 较高")
    if dataset.source_modified_at:
        modified = dataset.source_modified_at
        if isinstance(modified, str):
            modified = datetime.fromisoformat(modified)
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=timezone.utc)
        days = (now - modified).days
        if days <= 180:
            score += 1
            reasons.append("近半年有更新")
        else:
            reasons.append(f"超过 {days // 30} 个月未更新")
    else:
        reasons.append("来源更新日期未知")
    level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    return {"level": level, "score": score, "reasons": reasons, "label": "目录信息完整度", "measurement_kind": "metadata_heuristic"}


def _card_evidence_id(session, dataset_id: int, dataset_version_id: int | None) -> list[int]:
    return list(session.scalars(select(Evidence.id).where(Evidence.dataset_id == dataset_id,
        Evidence.dataset_version_id == dataset_version_id, Evidence.kind == "card"))) if dataset_version_id else []


def enrich_version(session, version: DatasetVersion) -> list[Enrichment]:
    from types import SimpleNamespace
    if version.provenance_status != "verified" or not version.metadata_json:
        from .provenance import RECOVERY_HELP
        raise ValueError(RECOVERY_HELP)
    item = version_to_dict(session, version)
    item.setdefault("source_modified_at", None)
    evidence_ids = _card_evidence_id(session, version.dataset_id, version.id)
    # Retain reviewed/historical outputs and preserve references from reviews and snapshots.
    for old in session.scalars(select(Enrichment).where(Enrichment.dataset_version_id == version.id,
            Enrichment.kind.in_(["schema", "quality"]), Enrichment.status == "candidate")):
        old.status = "superseded"
    entries = [
        Enrichment(dataset_id=version.dataset_id, dataset_version_id=version.id, kind="schema", producer=PRODUCER,
                   value=schema_summary(item["card_text"]), confidence="", evidence_ids=evidence_ids),
        Enrichment(dataset_id=version.dataset_id, dataset_version_id=version.id, kind="quality", producer=PRODUCER,
                   value=quality_signals(SimpleNamespace(**item)), confidence="", evidence_ids=evidence_ids),
    ]
    session.add_all(entries)
    return entries


def enrich_dataset(session, dataset: Dataset, *, dataset_version_id: int | None = None) -> list[Enrichment]:
    version = session.get(DatasetVersion, dataset_version_id) if dataset_version_id else latest_version(session, dataset.id)
    if version is None or version.dataset_id != dataset.id:
        raise ValueError("数据集版本不存在或不属于该数据集")
    return enrich_version(session, version)


def enrich_all(engine: Engine, *, only_missing: bool = False) -> dict:
    """全库整理；only_missing=True 时只处理尚无 schema 候选的记录。返回统计。"""
    stats = {"processed": 0, "skipped": 0}
    with session_scope(engine) as session:
        datasets = session.scalars(select(Dataset).where(Dataset.status == "active")).all()
        for dataset in datasets:
            if only_missing:
                has_schema = session.scalar(
                    select(Enrichment.id).where(Enrichment.dataset_version_id == latest_version(session, dataset.id).id, Enrichment.kind == "schema", Enrichment.status == "candidate")
                )
                if has_schema is not None:
                    stats["skipped"] += 1
                    continue
            enrich_dataset(session, dataset)
            stats["processed"] += 1
    return stats


def embedding_text(dataset: Dataset) -> str:
    """嵌入输入文本：名称 + 任务 + 卡片开头。文本哈希用于变更检测。"""
    parts = [dataset.source_key, " ".join(dataset.task_categories), dataset.card_text[:2000]]
    return "\n".join(part for part in parts if part.strip())


def embedding_text_hash(dataset: Dataset) -> str:
    return hashlib.sha256(embedding_text(dataset).encode("utf-8")).hexdigest()
